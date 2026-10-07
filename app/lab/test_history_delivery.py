import hashlib
import tempfile
from email import policy
from email.parser import BytesParser
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import Field, Job, Report
from .pdf_signing import sign_issued_pdf
from .test_pdf_signing import demo_identity, sample_pdf


class HistoryDeliveryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("history-reviewer")
        self.client.force_login(self.user)
        self.job = Job.objects.create(
            owner=self.user, title="Visible job", customer="Alpha", sample_code="S1"
        )

    def report(self, approved=False):
        return Report.objects.create(
            job=self.job,
            revision=1,
            snapshot={
                "title": "Frozen report",
                "customer": "Original customer",
                "sample_code": "OLD-S1",
            },
            snapshot_sha256="x" * 64,
            created_by=self.user,
            approved_at=timezone.now() if approved else None,
        )

    def test_filters_cover_entire_history_and_preserve_pagination(self):
        for i in range(26):
            job = Job.objects.create(
                owner=self.user, title=f"Alpha {i}", customer="Alpha", sample_code="S1"
            )
            Field.objects.create(job=job, key="x", value="1", label="Value", updated_by=self.user)
        response = self.client.get(reverse("dashboard"), {"customer": "Alpha", "status": "review"})
        self.assertEqual(response.context["job_page"].paginator.count, 26)
        self.assertEqual(response.context["needs_review"], 26)
        self.assertEqual(response.context["field_total"], 26)
        self.assertContains(response, "customer=Alpha&amp;status=review&amp;page=2")
        self.assertEqual(
            self.client.get(reverse("dashboard"), {"status": "empty"})
            .context["job_page"]
            .paginator.count,
            1,
        )

    def test_dashboard_query_count_does_not_grow_per_visible_job(self):
        for i in range(25):
            job = Job.objects.create(owner=self.user, title=f"Load fixture {i}", customer="Fixture")
            Field.objects.create(
                job=job, key="reading", value="1", label="Reading", updated_by=self.user
            )
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("dashboard"), {"customer": "Fixture"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["job_page"].paginator.count, 25)
        self.assertEqual(response.context["jobs"][0].field_count, 1)
        self.assertLessEqual(
            len(queries), 15, "Dashboard should aggregate page progress, not query each job."
        )

    def test_invalid_date_range_is_visible_and_private_jobs_excluded(self):
        other = get_user_model().objects.create_user("private-owner")
        Job.objects.create(owner=other, title="Other private job", customer="Alpha")
        self.assertNotContains(self.client.get(reverse("dashboard")), "Other private job")
        response = self.client.get(
            reverse("dashboard"), {"date_from": "2026-09-30", "date_to": "2026-09-01"}
        )
        self.assertContains(response, "start date must be on or before")
        self.assertEqual(response.context["job_page"].paginator.count, 0)

    def test_archive_searches_frozen_customer_and_status(self):
        self.report()
        response = self.client.get(
            reverse("report_archive"), {"customer": "Original", "sample": "OLD", "status": "draft"}
        )
        self.assertEqual(response.context["page"].paginator.count, 1)
        self.assertEqual(
            self.client.get(reverse("report_archive"), {"status": "approved"})
            .context["page"]
            .paginator.count,
            0,
        )

    def test_delivery_requires_approved_pdf_and_owner_access(self):
        report = self.report()
        url = reverse("report_delivery", args=[report.pk])
        self.assertEqual(self.client.get(url).status_code, 400)
        self.client.force_login(get_user_model().objects.create_user("outside"))
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_email_draft_contains_frozen_pdf_without_marking_sent(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            report = self.report(True)
            report.approved_pdf.save("fixture.pdf", ContentFile(b"%PDF-frozen-fixture"))
            url = reverse("report_delivery", args=[report.pk])
            data = {
                "recipient": "recipient@example.test",
                "status": "pending",
                "version": 0,
                "action": "email_draft",
            }
            response = self.client.post(url, data)
            self.assertEqual(response.status_code, 200)
            message = BytesParser(policy=policy.default).parsebytes(response.content)
            self.assertEqual(message["To"], "recipient@example.test")
            self.assertEqual(
                next(message.iter_attachments()).get_payload(decode=True), b"%PDF-frozen-fixture"
            )
            report.refresh_from_db()
            self.assertEqual(report.delivery_status, "pending")
            data.update(
                action="save",
                status="sent",
                note="Sent through lab email at 10:30",
                confirmed="yes",
            )
            self.assertEqual(self.client.post(url, data).status_code, 302)
            self.assertEqual(self.client.post(url, data).status_code, 409)
            report.refresh_from_db()
            self.assertEqual(report.delivery_status, "sent")
            self.assertTrue(report.job.events.filter(action="delivery_status_recorded").exists())

    def test_signed_report_sends_once_through_explicit_opt_in(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(
                MEDIA_ROOT=directory,
                EMAIL_DELIVERY_ENABLED=True,
                EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                EMAIL_HOST="fixture",
                DEFAULT_FROM_EMAIL="lab@example.test",
            ),
        ):
            p12, _ = demo_identity(directory)
            with override_settings(
                PDF_SIGNING_P12_PATH=str(p12), PDF_SIGNING_P12_PASSWORD="fixture-password"
            ):
                pdf_bytes, signed = sign_issued_pdf(sample_pdf(), "Demo HoD")
            self.assertTrue(signed)
            report = self.report(True)
            report.approved_pdf.save("signed.pdf", ContentFile(pdf_bytes))
            report.approved_pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
            report.save(update_fields=["approved_pdf_sha256"])
            url = reverse("report_delivery", args=[report.pk])
            payload = {
                "recipient": "customer@example.test",
                "status": "pending",
                "version": 0,
                "action": "send_email",
                "confirmed": "yes",
                "recipient_checked": "yes",
            }
            self.assertEqual(self.client.post(url, payload).status_code, 302)
            report.refresh_from_db()
            self.assertEqual(report.delivery_status, "sent")
            self.assertEqual(len(mail.outbox), 1)
            self.assertEqual(mail.outbox[0].attachments[0][1], pdf_bytes)
            self.assertEqual(self.client.post(url, payload).status_code, 409)
            self.assertEqual(len(mail.outbox), 1)

    def test_smtp_failure_holds_delivery_for_reconciliation(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(
                MEDIA_ROOT=directory,
                EMAIL_DELIVERY_ENABLED=True,
                EMAIL_HOST="fixture",
                DEFAULT_FROM_EMAIL="lab@example.test",
            ),
        ):
            p12, _ = demo_identity(directory)
            with override_settings(
                PDF_SIGNING_P12_PATH=str(p12), PDF_SIGNING_P12_PASSWORD="fixture-password"
            ):
                pdf_bytes, _ = sign_issued_pdf(sample_pdf(), "Demo HoD")
            report = self.report(True)
            report.approved_pdf.save("signed.pdf", ContentFile(pdf_bytes))
            report.approved_pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
            report.save(update_fields=["approved_pdf_sha256"])
            url = reverse("report_delivery", args=[report.pk])
            payload = {
                "recipient": "customer@example.test",
                "status": "pending",
                "version": 0,
                "action": "send_email",
                "confirmed": "yes",
                "recipient_checked": "yes",
            }
            with patch("lab.delivery.DjangoEmailMessage.send", side_effect=TimeoutError) as sender:
                self.assertEqual(self.client.post(url, payload).status_code, 503)
                self.assertEqual(self.client.post(url, payload).status_code, 409)
            sender.assert_called_once()
            report.refresh_from_db()
            self.assertEqual(report.delivery_status, "sending")
            reconcile = {
                "recipient": "customer@example.test",
                "status": "pending",
                "version": 1,
                "action": "save",
                "confirmed": "yes",
                "note": "",
            }
            self.assertEqual(self.client.post(url, reconcile).status_code, 200)
            report.refresh_from_db()
            self.assertEqual(report.delivery_status, "sending")
            reconcile["note"] = "Mailbox checked; no delivery seen."
            self.assertEqual(self.client.post(url, reconcile).status_code, 302)

    def test_auto_send_rejects_unsigned_pdf_without_marking_delivery(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(
                MEDIA_ROOT=directory,
                EMAIL_DELIVERY_ENABLED=True,
                EMAIL_HOST="fixture",
                DEFAULT_FROM_EMAIL="lab@example.test",
            ),
        ):
            report = self.report(True)
            pdf_bytes = sample_pdf()
            report.approved_pdf.save("unsigned.pdf", ContentFile(pdf_bytes))
            report.approved_pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
            report.save(update_fields=["approved_pdf_sha256"])
            with patch("lab.delivery.DjangoEmailMessage.send") as sender:
                response = self.client.post(
                    reverse("report_delivery", args=[report.pk]),
                    {
                        "recipient": "customer@example.test",
                        "status": "pending",
                        "version": 0,
                        "action": "send_email",
                        "confirmed": "yes",
                        "recipient_checked": "yes",
                    },
                )
            self.assertEqual(response.status_code, 400)
            sender.assert_not_called()
            report.refresh_from_db()
            self.assertEqual(report.delivery_status, "pending")
