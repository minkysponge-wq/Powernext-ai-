"""Real-browser gate with a temporary Django test database and no paid AI."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice
from pyhanko.keys import load_certs_from_pemder
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko_certvalidator import ValidationContext

from .models import AuditEvent, Job, Report
from .pdf_text_check import verify_request_pdf_text
from .roles import ADMIN, CUSTOMER, ENGINEER, HOD, QUALITY
from .test_pdf_signing import demo_identity


class BrowserAcceptanceTests(StaticLiveServerTestCase):
    def test_customer_assignment_station_lock_and_draft(self):
        password = "Synthetic-browser-password-1234"
        users = {}
        for key, role in (
            ("customer", CUSTOMER),
            ("admin", ADMIN),
            ("engineer", ENGINEER),
            ("other_engineer", ENGINEER),
            ("quality", QUALITY),
            ("hod", HOD),
        ):
            user = get_user_model().objects.create_user(
                "browser-" + key, email="browser-" + key + "@example.test", password=password
            )
            user.groups.add(Group.objects.get(name=role))
            users[key] = user.username
            users[key + "_id"] = user.pk
            if key in ("admin", "quality", "hod"):
                users[key + "_otp_secret"] = TOTPDevice.objects.create(
                    user=user, name="acceptance authenticator", confirmed=True
                ).bin_key.hex()
            if key in ("engineer", "quality", "hod"):
                codename = {
                    "engineer": "lock_report",
                    "quality": "verify_report",
                    "hod": "approve_report",
                }[key]
                user.user_permissions.add(Permission.objects.get(codename=codename))
        root = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        environment.update(
            VECTORLAB_TEST_URL=self.live_server_url,
            VECTORLAB_TEST_PASSWORD=password,
            VECTORLAB_TEST_USERS=json.dumps(users),
            PAID_AI_ALLOWED="0",
        )
        with tempfile.TemporaryDirectory() as directory:
            p12, pem = demo_identity(directory)
            with override_settings(
                MFA_ENFORCED=True,
                PDF_SIGNING_P12_PATH=str(p12),
                PDF_SIGNING_P12_PASSWORD="fixture-password",
                MEDIA_ROOT=directory,
                EMAIL_DELIVERY_ENABLED=True,
                EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                EMAIL_HOST="fixture",
                DEFAULT_FROM_EMAIL="lab@example.test",
            ):
                completed = subprocess.run(
                    ["node", str(root / "benchmark/playwright_acceptance.cjs")],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=240,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
                result = json.loads(completed.stdout.strip().splitlines()[-1])
                job = Job.objects.get(pk=result["job_id"])
                self.assertEqual(job.file_number, result["file_number"])
                self.assertEqual(job.test_runs.get().status, "locked")
                self.assertTrue(
                    AuditEvent.objects.filter(job=job, action="draft_snapshot_created").exists()
                )
                self.assertEqual(
                    job.customer_notifications.filter(kind="all_tests_complete").count(), 1
                )
                report = Report.objects.filter(job=job).order_by("-revision").first()
                self.assertEqual(report.revision, 2)
                self.assertIsNotNone(report.approved_at)
                self.assertEqual(report.delivery_status, "sent")
                self.assertEqual(len(mail.outbox), 1)
                with report.approved_pdf.open("rb") as stream:
                    reader = PdfFileReader(stream)
                    self.assertEqual(len(reader.embedded_signatures), 1)
                    context = ValidationContext(
                        trust_roots=list(load_certs_from_pemder([str(pem)]))
                    )
                    verdict = validate_pdf_signature(reader.embedded_signatures[0], context)
                    self.assertTrue(verdict.intact and verdict.valid)
                evidence_dir = os.environ.get("VECTORLAB_DEMO_EVIDENCE_DIR")
                if evidence_dir:
                    destination = Path(evidence_dir)
                    destination.mkdir(parents=True, exist_ok=True)
                    with report.approved_pdf.open("rb") as stream:
                        (destination / "issued-SYNTHETIC.pdf").write_bytes(stream.read())
                    (destination / "demo-signer-public.pem").write_bytes(pem.read_bytes())
                    (destination / "synthetic-run.json").write_text(
                        json.dumps(
                            {
                                "job_id": str(job.pk),
                                "file_number": job.file_number,
                                "report_id": str(report.pk),
                                "report_revision": report.revision,
                                "engineer": report.engineer_locked_by.username,
                                "quality": report.quality_verified_by.username,
                                "hod": report.approved_by.username,
                                "signature_intact": verdict.intact,
                                "signature_valid": verdict.valid,
                                "email_backend": "in-memory test outbox",
                                "customer_notifications": list(
                                    job.customer_notifications.values("kind", "created_at")
                                ),
                            },
                            indent=2,
                            default=str,
                        ),
                        encoding="utf-8",
                    )
                with report.approved_pdf.open("rb") as stream:
                    self.assertEqual(
                        verify_request_pdf_text(stream.read(), job.request_snapshot), []
                    )
                client = Client()
                client.force_login(get_user_model().objects.get(username=users["engineer"]))
                field = job.fields.first()
                self.assertEqual(
                    client.post(
                        reverse("edit_field", args=[job.pk, field.pk]),
                        {"value": "unauthorised later change"},
                    ).status_code,
                    409,
                )
                self.assertTrue(
                    AuditEvent.objects.filter(job=job, action="report_returned").exists()
                )
                self.assertTrue(
                    AuditEvent.objects.filter(job=job, action="delivery_email_sent").exists()
                )
