import tempfile
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfWriter

from .models import Document, Field, Job, Report
from .tasks import inspect_document


class WorkflowTests(TestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.setting = override_settings(MEDIA_ROOT=self.storage.name)
        self.setting.enable()
        self.addCleanup(self.setting.disable)
        self.addCleanup(self.storage.cleanup)
        self.user = get_user_model().objects.create_user("engineer", password="testing-only-43")
        self.other = get_user_model().objects.create_user("other", password="testing-only-44")
        self.client.force_login(self.user)
        self.job = Job.objects.create(
            owner=self.user, title="Transformer test", customer="Test customer", sample_code="S-1"
        )

    def pdf(self):
        stream = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.write(stream)
        return SimpleUploadedFile("source.pdf", stream.getvalue(), content_type="application/pdf")

    def upload(self):
        with patch("lab.views.async_task") as task:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("upload", args=[self.job.pk]), {"file": self.pdf()}
                )
            return response, task.call_count

    def payload(self, **kwargs):
        return {
            "key": "rated_power",
            "label": "Rated power",
            "value": "250",
            "unit": "kVA",
            "origin": "digital",
            "status": "verified",
            "version": 0,
            **kwargs,
        }

    def test_auth_required(self):
        self.client.logout()
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_job_persistence_and_search(self):
        response = self.client.post(
            "/jobs/new/",
            {"title": "Unique job", "customer": "Acme", "sample_code": "ABC", "test_series": "T1"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get("/?q=ABC"), "Unique job")
        self.assertNotContains(self.client.get("/?q=absent"), "Unique job")

    def test_duplicate_upload_only_queues_once(self):
        self.assertEqual(self.upload()[1], 1)
        self.assertEqual(self.upload()[1], 0)
        self.assertEqual(self.job.documents.count(), 1)

    def test_bad_pdf_rejected(self):
        self.client.post(
            reverse("upload", args=[self.job.pk]),
            {"file": SimpleUploadedFile("fake.pdf", b"<html>bad</html>")},
        )
        self.assertFalse(self.job.documents.exists())

    def test_late_report_lock_blocks_upload_and_reading_change(self):
        # The state can change while an upload is parsed or a form is validated.
        with patch("lab.views.job_locked", side_effect=[False, True]):
            response = self.client.post(reverse("upload", args=[self.job.pk]), {"file": self.pdf()})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.job.documents.exists())
        with patch("lab.views.job_locked", side_effect=[False, True]):
            response = self.client.post(reverse("new_field", args=[self.job.pk]), self.payload())
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.job.fields.exists())

    def test_job_and_file_permissions(self):
        self.upload()
        doc = self.job.documents.get()
        self.client.force_login(self.other)
        for url in [reverse("job_detail", args=[self.job.pk]), reverse("source", args=[doc.pk])]:
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(
            self.client.post(reverse("make_report", args=[self.job.pk])).status_code, 404
        )

    def test_task_is_idempotent_and_does_not_invent_ocr(self):
        self.upload()
        doc = self.job.documents.get()
        inspect_document(doc.pk)
        self.assertEqual(inspect_document(doc.pk), "Already processed")
        doc.refresh_from_db()
        self.assertEqual(doc.status, "ready")
        self.assertIn("not enabled", doc.processing_note)
        self.assertEqual(self.job.fields.count(), 0)

    def test_digital_value_and_stale_update(self):
        self.client.post(reverse("new_field", args=[self.job.pk]), self.payload())
        field = self.job.fields.get()
        url = reverse("edit_field", args=[self.job.pk, field.pk])
        self.assertEqual(self.client.post(url, self.payload(value="300")).status_code, 302)
        self.assertEqual(self.client.post(url, self.payload(value="400")).status_code, 409)
        field.refresh_from_db()
        self.assertEqual(field.value, "300")
        self.assertEqual(field.raw_value, "250")

    def test_review_correction_cannot_rewrite_reading_provenance(self):
        self.upload()
        source = self.job.documents.get()
        self.client.post(
            reverse("new_field", args=[self.job.pk]), self.payload(status="unreviewed")
        )
        field = self.job.fields.get()
        url = reverse("edit_field", args=[self.job.pk, field.pk])
        changed = self.payload(
            key="forged",
            label="Forged label",
            value="300",
            origin="scan",
            document=source.pk,
            page=1,
        )
        self.assertEqual(self.client.post(url, changed).status_code, 302)
        field.refresh_from_db()
        self.assertEqual(
            (field.key, field.label, field.origin, field.document_id, field.page),
            ("rated_power", "Rated power", "digital", None, None),
        )
        self.assertEqual((field.raw_value, field.value, field.status), ("250", "300", "verified"))

    def test_admin_cannot_edit_source_records_without_application_audit(self):
        from django.contrib import admin

        from .models import Rule

        self.assertFalse(admin.site._registry[Job].has_change_permission(None))
        self.assertFalse(admin.site._registry[Document].has_change_permission(None))
        self.assertFalse(admin.site._registry[Field].has_change_permission(None))
        self.assertFalse(admin.site._registry[Rule].has_change_permission(None))

    def test_blank_not_verified(self):
        self.client.post(reverse("new_field", args=[self.job.pk]), self.payload(value=""))
        self.assertEqual(self.job.fields.count(), 0)

    def test_scan_requires_evidence(self):
        self.client.post(reverse("new_field", args=[self.job.pk]), self.payload(origin="scan"))
        self.assertEqual(self.job.fields.count(), 0)

    def test_cross_job_source_rejected(self):
        self.upload()
        doc = self.job.documents.get()
        second = Job.objects.create(owner=self.user, title="Second", customer="Another")
        self.client.post(
            reverse("new_field", args=[second.pk]),
            self.payload(origin="scan", document=doc.pk, page=1),
        )
        self.assertEqual(second.fields.count(), 0)

    def test_report_snapshot_preserved_and_duplicate_request_reused(self):
        self.client.post(reverse("new_field", args=[self.job.pk]), self.payload())
        url = reverse("make_report", args=[self.job.pk])
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(Report.objects.count(), 1)
        report = Report.objects.get()
        Field.objects.filter(job=self.job).update(value="500")
        response = self.client.get(reverse("report", args=[report.pk]))
        self.assertContains(response, "250")
        self.assertContains(response, "DRAFT")
        self.client.post(url)
        self.assertEqual(Report.objects.count(), 2)

    def test_csrf_enforced(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post("/jobs/new/", {}).status_code, 403)

    def test_pdf_export_is_private_and_preserves_snapshot(self):
        from pypdf import PdfReader

        self.client.post(reverse("new_field", args=[self.job.pk]), self.payload())
        self.client.post(reverse("make_report", args=[self.job.pk]))
        report = Report.objects.get()
        Field.objects.filter(job=self.job).update(value="999")
        url = reverse("report_pdf", args=[report.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        text = "".join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
        self.assertIn("250", text)
        self.assertNotIn("999 kVA", text)
        self.assertIn("DRAFT", text)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_rules_and_results_are_frozen_in_report(self):
        from .models import Rule

        self.client.post(reverse("new_field", args=[self.job.pk]), self.payload())
        rule = Rule.objects.create(
            code="FIXTURE",
            title="Fixture maximum",
            operation="maximum",
            parameters={"fields": ["rated_power"], "unit": "kVA", "upper": 300},
        )
        self.job.rules.add(rule)
        self.client.post(reverse("make_report", args=[self.job.pk]))
        report = Report.objects.get()
        self.assertEqual(report.snapshot["calculations"][0]["state"], "within_limits")
        rule.parameters["upper"] = 200
        rule.save()
        report.refresh_from_db()
        self.assertEqual(report.snapshot["calculations"][0]["parameters"]["upper"], 300)

    def test_rule_assignment_version_and_permissions(self):
        from .models import Rule

        rule = Rule.objects.create(
            code="TEST",
            title="Test",
            operation="maximum",
            parameters={"fields": ["x"], "unit": "V"},
        )
        url = reverse("job_rules", args=[self.job.pk])
        self.assertEqual(self.client.post(url, {"rules": [rule.pk], "version": 0}).status_code, 302)
        self.assertEqual(self.client.post(url, {"rules": [], "version": 0}).status_code, 409)
        self.assertEqual(self.job.rules.count(), 1)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(url, {"rules": [], "version": 1}).status_code, 404)

    def test_guided_steps_and_real_review_progress(self):
        self.client.post(
            reverse("new_field", args=[self.job.pk]), self.payload(status="unreviewed")
        )
        page = self.client.get(reverse("job_detail", args=[self.job.pk]))
        self.assertContains(page, "Review exceptions by table")
        self.assertContains(page, "1 readings still need your attention")
        for step, label in [
            ("upload", "Add source forms"),
            ("results", "Calculation & limit checks"),
            ("report", "Saved revisions"),
        ]:
            response = self.client.get(reverse("job_detail", args=[self.job.pk]) + "?step=" + step)
            self.assertContains(response, label)
        dashboard = self.client.get("/")
        self.assertContains(dashboard, "0 of 1 readings")
