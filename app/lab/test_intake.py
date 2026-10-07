from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from reportlab.pdfgen import canvas

from .intake import classify_item
from .models import Document, IntakeItem, Job


def pdf_with_text(text):
    output = BytesIO()
    page = canvas.Canvas(output)
    page.drawString(40, 700, text)
    page.save()
    return output.getvalue()


class BatchIntakeTests(TestCase):
    def setUp(self):
        self.private = TemporaryDirectory()
        self.addCleanup(self.private.cleanup)
        self.media = override_settings(
            MEDIA_ROOT=self.private.name, EXTRACTION_PROVIDER="gemini", PAID_AI_ALLOWED=False
        )
        self.media.enable()
        self.addCleanup(self.media.disable)
        self.admin = get_user_model().objects.create_superuser(
            "batch-admin", "batch@example.invalid", "private-test-password-123"
        )
        self.other = get_user_model().objects.create_user(
            "batch-other", password="private-test-password-456"
        )
        self.job = Job.objects.create(
            owner=self.admin,
            title="Sample job",
            customer="Example",
            sample_code="S-001",
            test_series="T-001",
        )
        self.client.force_login(self.admin)

    def upload(self, content, name="source.pdf"):
        with patch("lab.intake.async_task") as task:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("batch_intake"), {"files": [SimpleUploadedFile(name, content)]}
                )
            return response, task

    def test_exact_text_identity_and_unique_form_routes_without_paid_ai(self):
        response, task = self.upload(pdf_with_text("S-001 T-001 Short Circuit Test"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(task.call_count, 1)
        item = IntakeItem.objects.get()
        self.assertEqual(classify_item(item.pk), "routed")
        doc = Document.objects.get(job=self.job)
        self.assertEqual(doc.form_type, "short_circuit")
        self.assertEqual(doc.status, "ready")
        item.refresh_from_db()
        self.assertEqual(item.page_count, 1)
        self.assertEqual(item.routed_document_id, doc.pk)
        self.assertEqual(classify_item(item.pk), "routed")
        self.assertEqual(Document.objects.count(), 1)

    def test_uncertain_document_requires_admin_triage(self):
        self.upload(pdf_with_text("Short Circuit Test"))
        item = IntakeItem.objects.get()
        self.assertEqual(classify_item(item.pk), "triage")
        self.assertFalse(Document.objects.exists())
        response = self.client.post(
            reverse("triage_item", args=[item.pk]),
            {"job_id": str(self.job.pk), "form_type": "short_circuit"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Document.objects.get().form_type, "short_circuit")
        item.refresh_from_db()
        self.assertEqual(item.status, "routed")

    def test_non_admin_cannot_intake_route_or_download(self):
        self.upload(pdf_with_text("Short Circuit Test"))
        item = IntakeItem.objects.get()
        classify_item(item.pk)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("batch_intake")).status_code, 403)
        self.assertEqual(self.client.get(reverse("intake_source", args=[item.pk])).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("triage_item", args=[item.pk]),
                {"job_id": str(self.job.pk), "form_type": "short_circuit"},
            ).status_code,
            403,
        )
        self.assertFalse(Document.objects.exists())

    def test_zip_rejects_nested_paths(self):
        archive = BytesIO()
        with ZipFile(archive, "w") as zip_file:
            zip_file.writestr("../source.pdf", pdf_with_text("S-001 T-001 Short Circuit Test"))
        response, task = self.upload(archive.getvalue(), "batch.zip")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(IntakeItem.objects.exists())
        self.assertEqual(task.call_count, 0)

    def test_route_conflict_stays_in_triage_and_older_jobs_are_searchable(self):
        self.upload(pdf_with_text("S-001 T-001 Short Circuit Test"))
        item = IntakeItem.objects.get()
        with patch("lab.intake.route_item", side_effect=ValueError("Job locked")):
            self.assertEqual(classify_item(item.pk), "triage")
        item.refresh_from_db()
        self.assertEqual(item.page_count, 1)
        self.assertEqual(item.status, "triage")
        self.assertFalse(Document.objects.exists())
        response = self.client.get(reverse("batch_intake"), {"q": "S-001"})
        self.assertContains(response, str(self.job.pk))
