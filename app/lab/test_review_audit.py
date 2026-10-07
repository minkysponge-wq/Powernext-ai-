import csv
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import reverse

from .models import AuditEvent, Document, Field, Job, TestRun
from .roles import ENGINEER


class ReviewAuditTests(TestCase):
    def test_scan_review_autosave_is_audited_and_versions_advance(self):
        user = get_user_model().objects.create_user("scan-autosave-engineer")
        user.groups.add(Group.objects.get(name=ENGINEER))
        job = Job.objects.create(owner=user, title="Scan fixture", customer="Fixture")
        TestRun.objects.create(
            job=job, test_type="work_instruction", assigned_to=user, status="in_progress"
        )
        document = Document.objects.create(
            job=job,
            original_name="scan.pdf",
            file="sources/demo.pdf",
            sha256="a" * 64,
            page_count=1,
            form_type="work_instruction",
            status="ready",
        )
        field = Field.objects.create(
            job=job,
            document=document,
            key="sample_code",
            label="Sample code",
            value="A1",
            raw_value="A1",
            updated_by=user,
            context={"form_type": "work_instruction", "schema_key": "sample_code"},
        )
        self.client.force_login(user)
        url = reverse("review_document", args=[document.pk])
        data = {
            "form-TOTAL_FORMS": 1,
            "form-INITIAL_FORMS": 1,
            "form-MIN_NUM_FORMS": 0,
            "form-MAX_NUM_FORMS": 1000,
            "form-0-id": field.pk,
            "form-0-value": "A1",
            "form-0-unit": "",
            "form-0-status": "verified",
            "form-0-version": field.version,
            "action": "save",
        }
        result = self.client.post(url, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["versions"][str(field.pk)], 1)
        field.refresh_from_db()
        self.assertEqual(field.status, "verified")
        self.assertTrue(AuditEvent.objects.filter(job=job, action="reading_updated").exists())

    def test_first_pass_filter_keeps_remaining_exceptions_visible(self):
        user = get_user_model().objects.create_user("review-fixture")
        job = Job.objects.create(owner=user, title="Fixture", customer="Fixture")
        for key, label in [("sample_code", "Sample code"), ("ordinary_note", "Routine note")]:
            Field.objects.create(
                job=job,
                key=key,
                label=label,
                value="Recorded",
                raw_value="Recorded",
                updated_by=user,
                context={"form_type": "work_instruction", "schema_key": key},
            )
        self.client.force_login(user)
        url = reverse("exception_review", args=[job.pk])
        self.assertContains(self.client.get(url), "Routine note")
        focused = self.client.get(url + "?focus=priority")
        self.assertContains(focused, "Sample code")
        self.assertNotContains(focused, "Routine note")
        self.assertContains(focused, "other 1 exceptions still require review")

    def test_export_is_blind_and_does_not_overwrite_review_work(self):
        user = get_user_model().objects.create_user("audit-fixture")
        job = Job.objects.create(owner=user, title="Fixture", customer="Fixture")
        Field.objects.create(
            job=job,
            key="sample_code",
            label="Sample",
            value="SECRET-PREDICTION",
            raw_value="SECRET-PREDICTION",
            updated_by=user,
            context={"form_type": "work_instruction", "schema_key": "sample_code"},
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "review"
            call_command(
                "export_review_audit", job=str(job.pk), output=destination, stdout=StringIO()
            )
            contents = (destination / "labels-to-check.csv").read_text(encoding="utf-8-sig")
            self.assertNotIn("SECRET-PREDICTION", contents)
            row = list(csv.DictReader(StringIO(contents)))[0]
            self.assertEqual(row["expected_value"], "")
            self.assertEqual(row["independently_verified"], "")
            self.assertEqual(row["unseen"], "")
            with self.assertRaises(CommandError):
                call_command(
                    "export_review_audit", job=str(job.pk), output=destination, stdout=StringIO()
                )
