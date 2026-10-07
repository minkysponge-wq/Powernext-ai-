"""Synthetic workflow acceptance through application endpoints, in an isolated DB."""

import csv
import hashlib
import io
import json
import os
import tempfile
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .extraction import get_schema
from .models import Job


class FullAcceptanceTest(TestCase):
    def test_import_review_approve_archive_and_delivery(self):
        started = time.perf_counter()
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            user = get_user_model().objects.create_user("synthetic-acceptance-reviewer")
            user.user_permissions.add(Permission.objects.get(codename="lock_report"))
            self.client.force_login(user)
            response = self.client.post(
                reverse("new_job"),
                {
                    "title": "SYNTHETIC workflow acceptance - not laboratory evidence",
                    "customer": "Synthetic customer",
                    "sample_code": "SYN-001",
                    "test_series": "SYN-T1",
                },
            )
            self.assertEqual(response.status_code, 302)
            job = Job.objects.get()
            kinds = ["work_instruction", "pressure_oil_leakage"]
            scope = {
                "customer": job.customer,
                "sample_code": job.sample_code,
                "test_series": job.test_series,
                "report_scope": kinds,
                "scope_note": "Synthetic oil-leakage observation workflow only. No real transformer or conformity decision. Other test sections excluded.",
                "version": job.version,
            }
            self.assertEqual(
                self.client.post(reverse("job_scope", args=[job.pk]), scope).status_code, 302
            )
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(["form_type", "page", "key", "value", "unit"])
            values = {
                "sample_code": job.sample_code,
                "sample_pressure": job.sample_code,
                "test_series": job.test_series,
                "series": job.test_series,
                "customer": job.customer,
                "oil_observation": "No leakage observed - SYNTHETIC fixture",
                "oil_pressure": "25",
                "oil_duration": "1",
                "oil_date": "2026-09-27",
            }
            for kind in kinds:
                for field in get_schema(kind)["fields"]:
                    key = field["key"]
                    writer.writerow(
                        [
                            kind,
                            field["page"],
                            key,
                            values.get(key, ""),
                            {"oil_pressure": "kPa", "oil_duration": "h"}.get(key, ""),
                        ]
                    )
            content = stream.getvalue().encode()
            url = reverse("import_readings", args=[job.pk])

            def upload():
                return self.client.post(
                    url,
                    {
                        "file": SimpleUploadedFile("synthetic-input.csv", content),
                        "source_reference": "Known synthetic fixture - not organiser data",
                    },
                )

            self.assertEqual(upload().status_code, 302)
            count = job.fields.count()
            self.assertEqual(upload().status_code, 302)
            self.assertEqual(job.fields.count(), count)
            self.client.post(reverse("make_report", args=[job.pk]))
            draft = job.reports.first()
            self.assertEqual(
                self.client.post(
                    reverse("engineer_lock", args=[draft.pk]), {"confirmed": "yes"}
                ).status_code,
                400,
            )
            fields = list(job.fields.order_by("key"))
            for offset in range(0, len(fields), 30):
                rows = fields[offset : offset + 30]
                data = {
                    "form-TOTAL_FORMS": len(rows),
                    "form-INITIAL_FORMS": len(rows),
                    "form-MIN_NUM_FORMS": 0,
                    "form-MAX_NUM_FORMS": 1000,
                }
                for i, f in enumerate(rows):
                    data.update(
                        {
                            f"form-{i}-id": f.pk,
                            f"form-{i}-value": f.value,
                            f"form-{i}-unit": f.unit,
                            f"form-{i}-status": "verified" if f.value else "not_applicable",
                            f"form-{i}-version": f.version,
                        }
                    )
                self.assertEqual(
                    self.client.post(
                        reverse("digital_review", args=[job.pk]) + f"?page={offset//30+1}", data
                    ).status_code,
                    302,
                )
            self.client.post(reverse("make_report", args=[job.pk]))
            report = job.reports.first()
            self.assertEqual(report.snapshot["blockers"], 0)
            self.assertEqual(
                self.client.post(
                    reverse("engineer_lock", args=[report.pk]), {"confirmed": "yes"}
                ).status_code,
                302,
            )
            quality = get_user_model().objects.create_user("synthetic-quality")
            quality.user_permissions.add(Permission.objects.get(codename="verify_report"))
            self.client.force_login(quality)
            self.assertEqual(
                self.client.post(
                    reverse("quality_verify", args=[report.pk]), {"confirmed": "yes"}
                ).status_code,
                302,
            )
            hod = get_user_model().objects.create_user("synthetic-hod")
            hod.user_permissions.add(Permission.objects.get(codename="approve_report"))
            self.client.force_login(hod)
            with patch("lab.pdf_text_check.verify_request_pdf_text", return_value=["manufacturer"]):
                self.assertEqual(
                    self.client.post(
                        reverse("approve_report", args=[report.pk]), {"confirmed": "yes"}
                    ).status_code,
                    409,
                )
            report.refresh_from_db()
            self.assertIsNone(report.approved_at)
            self.assertEqual(
                self.client.post(
                    reverse("approve_report", args=[report.pk]), {"confirmed": "yes"}
                ).status_code,
                302,
            )
            report.refresh_from_db()
            self.assertTrue(report.approved_at)
            pdf = b"".join(
                self.client.get(reverse("report_pdf", args=[report.pk])).streaming_content
            )
            evidence = self.client.get(reverse("report_evidence", args=[report.pk])).content
            self.assertEqual(len(list(csv.reader(io.StringIO(evidence.decode())))) - 1, count)
            self.assertContains(
                self.client.get(
                    reverse("report_archive"), {"sample": "SYN-001", "status": "approved"}
                ),
                "SYNTHETIC workflow acceptance",
            )
            delivery = reverse("report_delivery", args=[report.pk])
            payload = {
                "recipient": "synthetic@example.test",
                "status": "pending",
                "version": 0,
                "action": "email_draft",
            }
            email = self.client.post(delivery, payload)
            self.assertEqual(email.status_code, 200)
            attachment = next(
                BytesParser(policy=policy.default).parsebytes(email.content).iter_attachments()
            )
            self.assertEqual(attachment.get_payload(decode=True), pdf)
            report.refresh_from_db()
            self.assertEqual(report.delivery_status, "pending")
            self.assertFalse(report.job.events.filter(action="delivery_status_recorded").exists())
            result = {
                "case": "Synthetic application workflow, not OCR or engineering accuracy",
                "fields": count,
                "reviewed_values": job.fields.filter(status="verified").count(),
                "explicitly_not_applicable": job.fields.filter(status="not_applicable").count(),
                "approval_blocked_before_review": True,
                "approved_after_review": True,
                "archive_retrieval": True,
                "evidence_rows": count,
                "email_attachment_matches_frozen_pdf": True,
                "email_sent": False,
                "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
                "automated_test_seconds": time.perf_counter() - started,
                "limitations": "Isolated test database; synthetic limited test scope. No human-time, OCR accuracy or institutional certification claim.",
            }
            destination = os.getenv("VECTORLAB_ACCEPTANCE_OUTPUT")
            if destination:
                out = Path(destination)
                out.mkdir(parents=True, exist_ok=True)
                (out / "synthetic-approved-report.pdf").write_bytes(pdf)
                (out / "synthetic-evidence.csv").write_bytes(evidence)
                (out / "synthetic-input.csv").write_bytes(content)
                (out / "synthetic-unsent-email.eml").write_bytes(email.content)
                (out / "acceptance-results.json").write_text(
                    json.dumps(result, indent=2), encoding="utf-8"
                )
