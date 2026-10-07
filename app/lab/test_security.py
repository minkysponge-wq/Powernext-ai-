import subprocess
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from .models import AuditEvent, Document, Field, InsightDraft, Job, Report, TestRun
from .roles import ADMIN, CUSTOMER, ENGINEER, HOD, QUALITY


@override_settings(MFA_ENFORCED=True)
class PrivilegedMFATests(TestCase):
    def test_privileged_roles_need_otp_before_any_job_or_admin_route(self):
        for name in (HOD, QUALITY, ADMIN):
            user = get_user_model().objects.create_user(
                "mfa-" + name.lower(), password="Strong-test-password-1234"
            )
            user.groups.add(Group.objects.get(name=name))
            self.client.force_login(user)
            for path in (reverse("dashboard"), reverse("review_queue"), "/admin/"):
                with self.subTest(role=name, path=path):
                    self.assertEqual(self.client.get(path).status_code, 302)
                    self.assertIn(reverse("otp_verify"), self.client.get(path)["Location"])
            self.client.logout()

    def test_valid_totp_unlocks_session_and_external_next_is_rejected(self):
        user = get_user_model().objects.create_user(
            "mfa-quality", password="Strong-test-password-1234"
        )
        user.groups.add(Group.objects.get(name=QUALITY))
        device = TOTPDevice.objects.create(user=user, name="test device", confirmed=True)
        self.client.force_login(user)
        self.assertEqual(self.client.post(reverse("otp_verify"), {"token": "bad"}).status_code, 200)
        code = str(totp(device.bin_key)).zfill(6)
        response = self.client.post(
            reverse("otp_verify"), {"token": code, "next": "https://evil.invalid/"}
        )
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_missing_device_fails_closed(self):
        user = get_user_model().objects.create_superuser(
            "mfa-admin", "admin@example.invalid", "Strong-test-password-1234"
        )
        self.client.force_login(user)
        self.assertContains(
            self.client.get(reverse("otp_verify")), "No authenticator is registered"
        )
        self.assertIn(reverse("otp_verify"), self.client.get("/admin/")["Location"])

    @override_settings(PRIVILEGED_IDLE_SECONDS=1)
    def test_privileged_idle_session_expires(self):
        user = get_user_model().objects.create_user(
            "idle-quality", password="Strong-test-password-1234"
        )
        user.groups.add(Group.objects.get(name=QUALITY))
        self.client.force_login(user)
        session = self.client.session
        session["privileged_last_activity"] = 0
        session.save()
        self.assertRedirects(
            self.client.get(reverse("dashboard")), reverse("login"), fetch_redirect_response=False
        )
        self.assertNotIn("_auth_user_id", self.client.session)


class PasswordAndLockoutTests(TestCase):
    def test_minimum_length_and_common_password_rejected(self):
        for password in ("Short123!", "password123456"):
            with self.subTest(password=password), self.assertRaises(ValidationError):
                validate_password(password)

    @override_settings(AXES_FAILURE_LIMIT=3)
    def test_three_bad_passwords_lock_out_even_correct_password(self):
        get_user_model().objects.create_user("lockout-user", password="Strong-test-password-1234")
        for _ in range(3):
            self.client.post(
                reverse("login"), {"username": "lockout-user", "password": "incorrect"}
            )
        response = self.client.post(
            reverse("login"), {"username": "lockout-user", "password": "Strong-test-password-1234"}
        )
        self.assertEqual(response.status_code, 429)


class ApprovalSeparationTests(TestCase):
    def test_hod_cannot_issue_when_same_person_locked_or_quality_verified(self):
        signer = get_user_model().objects.create_superuser(
            "same-person", "same@example.invalid", "Strong-test-password-1234"
        )
        independent = get_user_model().objects.create_user("independent")
        self.client.force_login(signer)
        for engineer, quality in ((signer, independent), (independent, signer)):
            job = Job.objects.create(owner=signer, title="Separation fixture", customer="Fixture")
            report = Report.objects.create(
                job=job,
                revision=1,
                snapshot={},
                snapshot_sha256="0" * 64,
                created_by=signer,
                engineer_locked_by=engineer,
                engineer_locked_at=timezone.now(),
                quality_verified_by=quality,
                quality_verified_at=timezone.now(),
            )
            with self.subTest(engineer=engineer.pk, quality=quality.pk):
                self.assertEqual(
                    self.client.post(
                        reverse("approve_report", args=[report.pk]), {"confirmed": "yes"}
                    ).status_code,
                    403,
                )
                report.refresh_from_db()
                self.assertIsNone(report.approved_at)


class ObjectBoundaryMatrixTests(TestCase):
    def test_unrelated_customer_and_engineer_cannot_read_or_mutate_object_urls(self):
        owner = get_user_model().objects.create_user("boundary-owner")
        owner.groups.add(Group.objects.get(name=ENGINEER))
        linked_customer = get_user_model().objects.create_user("boundary-customer")
        linked_customer.groups.add(Group.objects.get(name=CUSTOMER))
        job = Job.objects.create(
            owner=owner,
            customer_user=linked_customer,
            file_number="PRIVATE-CASE-001",
            title="Private case",
            customer="Private",
        )
        document = Document.objects.create(
            job=job,
            original_name="private.pdf",
            file="sources/private.pdf",
            sha256="a" * 64,
            page_count=1,
            status="ready",
        )
        field = Field.objects.create(
            job=job,
            key="private-reading",
            label="Private reading",
            value="12",
            raw_value="12",
            origin="scan",
            document=document,
            page=1,
            bbox=[0.1, 0.1, 0.2, 0.2],
            updated_by=owner,
        )
        report = Report.objects.create(
            job=job,
            revision=1,
            snapshot={"title": "Private"},
            snapshot_sha256="0" * 64,
            created_by=owner,
        )
        run = TestRun.objects.create(job=job, test_type="work_instruction", assigned_to=owner)
        insight = InsightDraft.objects.create(job=job, evidence_hash="0" * 64)
        get_paths = [
            ("job_detail", [job.pk]),
            ("audit_history", [job.pk]),
            ("exception_review", [job.pk]),
            ("report_mapping", [job.pk]),
            ("job_scope", [job.pk]),
            ("import_readings", [job.pk]),
            ("import_template", [job.pk]),
            ("digital_review", [job.pk]),
            ("findings", [job.pk]),
            ("job_rules", [job.pk]),
            ("review_document", [document.pk]),
            ("transformer_results", [document.pk]),
            ("source", [document.pk]),
            ("source_page", [document.pk, 1]),
            ("field_crop", [field.pk]),
            ("report", [report.pk]),
            ("report_pdf", [report.pk]),
            ("report_evidence", [report.pk]),
            ("quality_review", [report.pk]),
            ("report_delivery", [report.pk]),
            ("customer_status", [job.file_number]),
            ("customer_download", [job.file_number]),
        ]
        post_paths = [
            ("upload", [job.pk]),
            ("new_field", [job.pk]),
            ("edit_field", [job.pk, field.pk]),
            ("make_report", [job.pk]),
            ("assign_test", [job.pk]),
            ("approve_table", [job.pk]),
            ("report_mapping", [job.pk]),
            ("job_scope", [job.pk]),
            ("import_readings", [job.pk]),
            ("digital_review", [job.pk]),
            ("findings", [job.pk]),
            ("job_rules", [job.pk]),
            ("review_document", [document.pk]),
            ("report_delivery", [report.pk]),
            ("request_insights", [job.pk]),
            ("retry_extraction", [document.pk]),
            ("engineer_lock", [report.pk]),
            ("quality_verify", [report.pk]),
            ("return_for_correction", [report.pk]),
            ("approve_report", [report.pk]),
            ("approve_insights", [insight.pk]),
            ("start_test", [run.pk]),
            ("lock_test", [run.pk]),
            ("mark_unused_not_applicable", [run.pk]),
            ("reopen_test", [run.pk]),
        ]
        for role in (CUSTOMER, ENGINEER):
            outsider = get_user_model().objects.create_user("other-" + role.replace(" ", "-"))
            outsider.groups.add(Group.objects.get(name=role))
            self.client.force_login(outsider)
            for method, paths in ((self.client.get, get_paths), (self.client.post, post_paths)):
                for name, args in paths:
                    with self.subTest(role=role, route=name):
                        response = method(reverse(name, args=args))
                        self.assertIn(response.status_code, (403, 404))


class AuditChainTests(TestCase):
    def test_append_only_and_detects_direct_database_tampering(self):
        user = get_user_model().objects.create_user("audit-user")
        job = Job.objects.create(owner=user, title="Audited", customer="Customer")
        first = AuditEvent.objects.create(
            job=job, actor=user, action="created", details={"value": "before"}
        )
        second = AuditEvent.objects.create(
            job=job, actor=user, action="reviewed", details={"value": "after"}
        )
        self.assertEqual(second.previous_hash, first.event_hash)
        call_command("verify_audit_chain", verbosity=0)
        with self.assertRaises(ValueError):
            first.delete()
        with self.assertRaises(ValueError):
            AuditEvent.objects.filter(pk=first.pk).update(action="altered")
        with connection.cursor() as cursor:
            cursor.execute("UPDATE lab_auditevent SET action=%s WHERE id=%s", ["altered", first.pk])
        with self.assertRaises(CommandError):
            call_command("verify_audit_chain", verbosity=0)

    def test_detects_deleted_tail_event(self):
        user = get_user_model().objects.create_user("audit-tail-user")
        job = Job.objects.create(owner=user, title="Tail test", customer="Customer")
        AuditEvent.objects.create(job=job, actor=user, action="first")
        tail = AuditEvent.objects.create(job=job, actor=user, action="last")
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM lab_auditevent WHERE id=%s", [tail.pk])
        with self.assertRaises(CommandError):
            call_command("verify_audit_chain", verbosity=0)


class PDFSafetyTests(TestCase):
    def test_rejects_fake_pdf_and_enforces_parse_timeout(self):
        from .pdf_safety import probe_pdf

        for payload in (b"not-a-pdf", b"%PDF-1.7\nmissing trailer"):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                probe_pdf(payload)
        with patch(
            "lab.pdf_safety.subprocess.run", side_effect=subprocess.TimeoutExpired("probe", 15)
        ):
            with self.assertRaisesRegex(ValueError, "time limit"):
                probe_pdf(b"%PDF-1.7\n%%EOF")

    def test_preview_render_timeout_is_bounded(self):
        from .extraction.rendering import render_page

        with patch(
            "lab.extraction.rendering.subprocess.run",
            side_effect=subprocess.TimeoutExpired("render", 20),
        ):
            with self.assertRaisesRegex(ValueError, "time limit"):
                render_page("fixture.pdf", 1)


class ProductionHeaderTests(TestCase):
    @override_settings(DEBUG=False, SECURE_SSL_REDIRECT=False)
    def test_production_404_includes_csp_and_nosniff(self):
        response = self.client.get("/missing-security-scan-path/")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Content-Security-Policy", response)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
