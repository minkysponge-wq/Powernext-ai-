import json
import tempfile
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from pypdf import PdfWriter

from .extraction import get_provider, get_schema
from .extraction.gemini import ExtractionError, GeminiProvider, _retry_delay, validate_fields
from .models import Document, Field, Job
from .tasks import inspect_document


class ProviderTests(SimpleTestCase):
    def test_cached_demo_replay_requires_exact_page_and_debug(self):
        import hashlib
        import json
        import tempfile
        from pathlib import Path

        from .extraction import get_provider
        from .extraction.gemini import PROMPT, ExtractionError

        schema = {"form_type": "fixture", "fields": [{"key": "reading", "page": 1}]}
        image = b"known page"
        prompt = PROMPT + json.dumps(
            {"form_type": "fixture", "page": 1, "instructions": [], "fields": schema["fields"]},
            sort_keys=True,
        )
        page = {
            "fields": [
                {
                    "key": "reading",
                    "page": 1,
                    "raw_text": "24.8",
                    "unit": "degC",
                    "status": "read",
                    "bbox": [1, 2, 3, 4],
                }
            ],
            "metadata": {
                "provider": "gemini",
                "image_sha256": hashlib.sha256(image).hexdigest(),
                "schema_sha256": hashlib.sha256(
                    json.dumps(schema, sort_keys=True).encode()
                ).hexdigest(),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "fixture-p1.json").write_text(json.dumps(page), encoding="utf-8")
            with override_settings(
                DEBUG=True, EXTRACTION_PROVIDER="cached_demo", DEMO_EXTRACTION_CACHE=Path(directory)
            ):
                provider = get_provider()
                self.assertEqual(
                    provider.extract_page(image, schema, 1)["metadata"]["demo_replay"], True
                )
                with self.assertRaises(ExtractionError):
                    provider.extract_page(b"changed page", schema, 1)
                with self.assertRaises(ExtractionError):
                    provider.extract_page(image, {**schema, "fields": []}, 1)
            with override_settings(
                DEBUG=True,
                EXTRACTION_PROVIDER="cached_demo",
                DEMO_EXTRACTION_CACHE=Path(directory),
                DEMO_ALLOW_LEGACY_CACHE=True,
            ):
                result = get_provider().extract_page(
                    image,
                    {**schema, "fields": schema["fields"] + [{"key": "new_reading", "page": 1}]},
                    1,
                )
                self.assertTrue(result["metadata"]["legacy_schema_replay"])
                self.assertEqual(result["fields"][1]["issue"], "added_after_cached_extraction")
            with override_settings(
                DEBUG=False,
                EXTRACTION_PROVIDER="cached_demo",
                DEMO_EXTRACTION_CACHE=Path(directory),
            ):
                with self.assertRaises(ExtractionError):
                    get_provider()

    def test_box_conversion_and_missing_fields(self):
        schema = {"fields": [{"key": "a", "page": 1}, {"key": "b", "page": 1}]}
        output = validate_fields(
            {
                "fields": [
                    {
                        "key": "a",
                        "raw_text": "12.0",
                        "unit": "V",
                        "status": "read",
                        "box_2d": [100, 200, 300, 400],
                    }
                ]
            },
            schema,
            1,
        )
        self.assertEqual(output[0]["bbox"], [0.2, 0.1, 0.4, 0.3])
        self.assertEqual(output[1]["status"], "missing")

    def test_invalid_identity_and_evidence_rejected(self):
        schema = {"fields": [{"key": "a", "page": 1}]}
        good = {"key": "a", "raw_text": "1", "unit": "V", "status": "read", "box_2d": [1, 2, 3, 4]}
        with self.assertRaises(ExtractionError):
            validate_fields({"fields": [{**good, "key": "invented"}]}, schema, 1)
        for change, issue in [
            ({"raw_text": ""}, "empty_reading"),
            ({"unit": "x" * 31}, "invalid_text_or_unit"),
            ({"status": "unknown"}, "invalid_status"),
        ]:
            withheld = validate_fields({"fields": [{**good, **change}]}, schema, 1)[0]
            self.assertEqual(
                (withheld["status"], withheld["raw_text"], withheld["issue"]),
                ("missing", "", issue),
            )
        for box, issue in [(None, "source_box_missing"), ([0, 0, 2000, 2], "invalid_source_box")]:
            withheld = validate_fields({"fields": [{**good, "box_2d": box}]}, schema, 1)[0]
            self.assertEqual(
                (withheld["status"], withheld["raw_text"], withheld["bbox"]), ("missing", "", None)
            )
            self.assertEqual(withheld["issue"], issue)
        self.assertEqual(
            validate_fields({"fields": [{**good, "raw_text": "x" * 10001}]}, schema, 1)[0]["issue"],
            "invalid_text_or_unit",
        )
        self.assertEqual(
            validate_fields({"fields": [good, good]}, schema, 1)[0]["issue"], "duplicate_key"
        )
        with self.assertRaises(ExtractionError):
            validate_fields({"fields": "not a list"}, schema, 1)

    def test_one_bad_reading_does_not_discard_other_page_evidence(self):
        schema = {"fields": [{"key": "a", "page": 1}, {"key": "b", "page": 1}]}
        good = {
            "key": "a",
            "raw_text": "12.0",
            "unit": "V",
            "status": "read",
            "box_2d": [100, 200, 300, 400],
        }
        bad = {
            "key": "b",
            "raw_text": "",
            "unit": "A",
            "status": "read",
            "box_2d": [100, 500, 300, 700],
        }
        fields = validate_fields({"fields": [good, bad]}, schema, 1)
        self.assertEqual(fields[0]["raw_text"], "12.0")
        self.assertEqual(fields[0]["status"], "read")
        self.assertEqual((fields[1]["status"], fields[1]["issue"]), ("missing", "empty_reading"))

    def test_http_error_does_not_expose_response_body(self):
        error = HTTPError("https://example.invalid", 403, "denied", {}, BytesIO(b"private content"))
        with patch("lab.extraction.gemini.urlopen", side_effect=error):
            with self.assertRaisesRegex(ExtractionError, "HTTP 403") as caught:
                GeminiProvider("test-key", "model").extract_page(
                    b"image", {"form_type": "test", "fields": [{"key": "a", "page": 1}]}, 1
                )
        self.assertNotIn("private content", str(caught.exception))

    def test_transient_provider_failure_retries_once_without_leaking_body(self):
        error = HTTPError("https://example.invalid", 503, "busy", {}, BytesIO(b"private content"))
        body = {
            "status": "completed",
            "steps": [
                {
                    "type": "model_output",
                    "content": [{"type": "text", "text": json.dumps({"fields": []})}],
                }
            ],
            "usage": {},
        }
        with (
            patch(
                "lab.extraction.gemini.urlopen",
                side_effect=[error, BytesIO(json.dumps(body).encode())],
            ) as call,
            patch("lab.extraction.gemini.time.sleep") as sleep,
        ):
            result = GeminiProvider("test-key", "model").extract_page(
                b"image", {"form_type": "test", "fields": [{"key": "a", "page": 1}]}, 1
            )
        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once_with(2)
        self.assertEqual(result["fields"][0]["status"], "missing")

    def test_thinking_level_is_explicit_and_recorded(self):
        body = {
            "status": "completed",
            "steps": [
                {
                    "type": "model_output",
                    "content": [{"type": "text", "text": json.dumps({"fields": []})}],
                }
            ],
            "usage": {},
        }
        with patch(
            "lab.extraction.gemini.urlopen", return_value=BytesIO(json.dumps(body).encode())
        ) as request:
            result = GeminiProvider("test-key", "model", "low").extract_page(
                b"image", {"form_type": "test", "fields": [{"key": "a", "page": 1}]}, 1
            )
        posted = json.loads(request.call_args.args[0].data)
        self.assertEqual(posted["generation_config"]["thinking_level"], "low")
        self.assertEqual(result["metadata"]["thinking_level"], "low")
        with self.assertRaises(ExtractionError):
            GeminiProvider("test-key", "model", "minimal")

    def test_malformed_provider_response_is_rejected_without_echoing_content(self):
        with patch("lab.extraction.gemini.urlopen", return_value=BytesIO(b"{private data")):
            with self.assertRaisesRegex(
                ExtractionError, "no usable structured extraction"
            ) as caught:
                GeminiProvider("test-key", "model").extract_page(
                    b"image", {"form_type": "test", "fields": [{"key": "a", "page": 1}]}, 1
                )
        self.assertNotIn("private data", str(caught.exception))

    def test_quota_and_long_retry_after_do_not_block_worker(self):
        for code, headers in [
            (429, {}),
            (429, {"Retry-After": "120"}),
            (503, {"Retry-After": "120"}),
            (403, {}),
        ]:
            self.assertIsNone(
                _retry_delay(HTTPError("https://example.invalid", code, "error", headers, None))
            )
        self.assertEqual(
            _retry_delay(
                HTTPError("https://example.invalid", 429, "busy", {"Retry-After": "1"}, None)
            ),
            1,
        )

    @override_settings(DEBUG=False, CLOUD_AI_ALLOWED=False, EXTRACTION_PROVIDER="gemini")
    def test_production_cloud_source_processing_requires_explicit_enablement(self):
        with self.assertRaisesRegex(ExtractionError, "lab authorisation"):
            get_provider()

    @override_settings(DEBUG=True, PAID_AI_ALLOWED=False, EXTRACTION_PROVIDER="gemini")
    def test_paid_extraction_requires_explicit_budget_switch(self):
        with self.assertRaisesRegex(ExtractionError, "Paid Gemini requests are off"):
            get_provider()


@override_settings(
    GEMINI_API_KEY="test-only",
    GEMINI_MODEL="test-model",
    EXTRACTION_PROVIDER="gemini",
    CLOUD_AI_ALLOWED=True,
    PAID_AI_ALLOWED=True,
)
class ExtractionWorkflowTests(TestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        settings = override_settings(MEDIA_ROOT=self.storage.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.user = get_user_model().objects.create_user("extractor")
        self.other = get_user_model().objects.create_user("outsider")
        self.client.force_login(self.user)
        self.job = Job.objects.create(owner=self.user, title="Extraction test", customer="Fixture")

    def upload(self):
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        stream = BytesIO()
        writer.write(stream)
        with patch("lab.views.async_task") as task:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("upload", args=[self.job.pk]),
                    {
                        "file": SimpleUploadedFile("test.pdf", stream.getvalue()),
                        "form_type": "work_instruction",
                    },
                )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(task.call_count, 1)
        return self.job.documents.get()

    @override_settings(DEBUG=False, CLOUD_AI_ALLOWED=False)
    def test_production_upload_does_not_queue_unapproved_cloud_transfer(self):
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        stream = BytesIO()
        writer.write(stream)
        with patch("lab.views.async_task") as task:
            response = self.client.post(
                reverse("upload", args=[self.job.pk]),
                {
                    "file": SimpleUploadedFile("sensitive.pdf", stream.getvalue()),
                    "form_type": "work_instruction",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.job.documents.exists())
        task.assert_not_called()

    @override_settings(PAID_AI_ALLOWED=False)
    def test_budget_pause_does_not_save_or_queue_scanned_upload(self):
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        stream = BytesIO()
        writer.write(stream)
        with patch("lab.views.async_task") as task:
            response = self.client.post(
                reverse("upload", args=[self.job.pk]),
                {
                    "file": SimpleUploadedFile("paused.pdf", stream.getvalue()),
                    "form_type": "work_instruction",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.job.documents.exists())
        task.assert_not_called()

    @override_settings(PAID_AI_ALLOWED=False)
    def test_budget_pause_does_not_retry_extraction(self):
        doc = Document.objects.create(
            job=self.job,
            original_name="existing.pdf",
            form_type="work_instruction",
            page_count=1,
            status="failed",
        )
        with patch("lab.views.async_task") as task:
            response = self.client.post(
                reverse("retry_extraction", args=[doc.pk]), {"form_type": "work_instruction"}
            )
        self.assertEqual(response.status_code, 403)
        doc.refresh_from_db()
        self.assertEqual(doc.status, "failed")
        task.assert_not_called()

    def test_original_source_is_downloaded_not_embedded_in_site(self):
        doc = self.upload()
        response = self.client.get(reverse("source", args=[doc.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;", response["Content-Disposition"])
        response.close()

    def response(self):
        field = {
            "key": "test_series",
            "raw_text": "FIX-123",
            "unit": "",
            "status": "read",
            "box_2d": [100, 200, 200, 600],
        }
        return BytesIO(
            json.dumps(
                {
                    "status": "completed",
                    "steps": [
                        {
                            "type": "model_output",
                            "content": [{"type": "text", "text": json.dumps({"fields": [field]})}],
                        }
                    ],
                    "model": "test-version",
                    "usage": {"total_tokens": 20},
                }
            ).encode()
        )

    def test_upload_to_extracted_fields_and_crop(self):
        doc = self.upload()
        with patch("lab.extraction.gemini.urlopen", return_value=self.response()) as request:
            inspect_document(doc.pk)
        doc.refresh_from_db()
        self.assertEqual(doc.status, "ready")
        self.assertEqual(
            doc.extraction_data["pages"][0]["metadata"]["model_version"], "test-version"
        )
        self.assertEqual(self.job.fields.count(), len(get_schema("work_instruction")["fields"]))
        self.assertFalse(self.job.fields.exclude(status="unreviewed").exists())
        field = self.job.fields.get(context__schema_key="test_series")
        self.assertEqual(field.value, "FIX-123")
        self.assertEqual(field.bbox, [0.2, 0.1, 0.6, 0.2])
        posted = json.loads(request.call_args.args[0].data)
        self.assertEqual(posted["input"][1]["type"], "image")
        self.assertNotIn("FIX-123", posted["input"][0]["text"])
        response = self.client.get(reverse("field_crop", args=[field.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"\x89PNG"))
        field.value = "Reviewed"
        field.status = "verified"
        field.save()
        self.assertEqual(inspect_document(doc.pk), "Already processed")
        field.refresh_from_db()
        self.assertEqual(field.value, "Reviewed")
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("field_crop", args=[field.pk])).status_code, 404)
        self.assertEqual(
            self.client.post(
                reverse("retry_extraction", args=[doc.pk]), {"form_type": "work_instruction"}
            ).status_code,
            404,
        )

    def test_provider_failure_creates_no_readings(self):
        doc = self.upload()
        with patch("lab.extraction.gemini.urlopen", side_effect=TimeoutError):
            inspect_document(doc.pk)
        doc.refresh_from_db()
        self.assertEqual(doc.status, "failed")
        self.assertEqual(self.job.fields.count(), 0)
        self.assertIn("timed out", doc.processing_note)

    def test_multpage_retry_reuses_completed_pages_without_partial_import(self):
        doc = self.upload()
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.add_blank_page(width=595, height=842)
        stream = BytesIO()
        writer.write(stream)
        from django.core.files.base import ContentFile

        doc.file.save("two-page.pdf", ContentFile(stream.getvalue()), save=False)
        doc.form_type = "temperature_rise"
        doc.page_count = 2
        doc.save()

        def result(page):
            return {
                "fields": validate_fields({"fields": []}, get_schema("temperature_rise"), page),
                "metadata": {"provider": "gemini", "model": "test-model", "page": page},
            }

        with (
            patch("lab.tasks.render_page", return_value=b"image"),
            patch(
                "lab.extraction.gemini.GeminiProvider.extract_page",
                side_effect=[result(1), ExtractionError("temporary outage")],
            ) as call,
        ):
            inspect_document(doc.pk)
            self.assertEqual(call.call_count, 2)
        doc.refresh_from_db()
        self.assertEqual(doc.status, "failed")
        self.assertEqual(self.job.fields.count(), 0)
        self.assertEqual(list(doc.extraction_checkpoint["pages"]), ["1"])
        with (
            patch("lab.tasks.render_page", return_value=b"image"),
            patch(
                "lab.extraction.gemini.GeminiProvider.extract_page", return_value=result(2)
            ) as call,
        ):
            inspect_document(doc.pk)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(call.call_args.args[2], 2)
        doc.refresh_from_db()
        self.assertEqual(doc.status, "ready")
        self.assertEqual(self.job.fields.count(), len(get_schema("temperature_rise")["fields"]))

    def test_checkpoint_does_not_cross_model_changes(self):
        doc = self.upload()
        with patch("lab.extraction.gemini.urlopen", return_value=self.response()):
            inspect_document(doc.pk)
        doc.refresh_from_db()
        self.assertTrue(doc.extraction_checkpoint)
        # Simulate an interrupted run retaining only the checkpoint.
        self.job.fields.all().delete()
        doc.extraction_data = {}
        doc.status = "failed"
        doc.save()
        with (
            override_settings(GEMINI_MODEL="different-model"),
            patch("lab.extraction.gemini.urlopen", return_value=self.response()) as call,
        ):
            inspect_document(doc.pk)
            self.assertEqual(call.call_count, 1)

    def test_checkpoint_does_not_reuse_changed_rendered_image(self):
        doc = self.upload()
        with (
            patch("lab.tasks.render_page", return_value=b"first image"),
            patch("lab.extraction.gemini.urlopen", return_value=self.response()),
        ):
            inspect_document(doc.pk)
        doc.refresh_from_db()
        self.job.fields.all().delete()
        doc.extraction_data = {}
        doc.status = "failed"
        doc.save()
        with (
            patch("lab.tasks.render_page", return_value=b"changed image"),
            patch("lab.extraction.gemini.urlopen", return_value=self.response()) as call,
        ):
            inspect_document(doc.pk)
            self.assertEqual(call.call_count, 1)

    def test_retry_after_failure(self):
        doc = self.upload()
        doc.status = "failed"
        doc.save()
        with patch("lab.views.async_task") as task:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("retry_extraction", args=[doc.pk]), {"form_type": "work_instruction"}
                )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(task.call_count, 1)

    def test_form_review_updates_with_original_preserved(self):
        doc = self.upload()
        field = Field.objects.create(
            job=self.job,
            document=doc,
            key="sample",
            label="Sample",
            value="old",
            raw_value="old",
            origin="scan",
            page=1,
            updated_by=self.user,
        )
        url = reverse("review_document", args=[doc.pk])
        self.assertContains(self.client.get(url), "Original source")
        self.assertContains(
            self.client.get(url), "Critical source value: individual review required."
        )
        payload = {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "1",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-id": field.pk,
            "form-0-value": "checked",
            "form-0-unit": "",
            "form-0-status": "verified",
            "form-0-version": "0",
        }
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        field.refresh_from_db()
        self.assertEqual(field.value, "checked")
        self.assertEqual(field.raw_value, "old")
        self.assertEqual(self.client.post(url, payload).status_code, 409)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_transformer_page_requires_reviewed_units(self):
        doc = self.upload()
        doc.form_type = "loss_calculation"
        doc.save()
        for key, value in [("material", "Cu"), ("phases", "3"), ("rated_power", "250")]:
            Field.objects.create(
                job=self.job,
                document=doc,
                key=key,
                label=key,
                value=value,
                unit="",
                origin="scan",
                page=1,
                status="verified",
                updated_by=self.user,
            )
        response = self.client.get(reverse("transformer_results", args=[doc.pk]))
        self.assertContains(response, "Confirm unit kVA for rated_power")

    def test_save_finish_and_private_page_preview(self):
        doc = self.upload()
        field = Field.objects.create(
            job=self.job,
            document=doc,
            key="reading",
            label="Reading",
            value="10",
            raw_value="10",
            unit="V",
            origin="scan",
            page=1,
            updated_by=self.user,
        )
        payload = {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "1",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-id": field.pk,
            "form-0-value": "10",
            "form-0-unit": "V",
            "form-0-status": "verified",
            "form-0-version": "0",
            "action": "next",
        }
        response = self.client.post(reverse("review_document", args=[doc.pk]), payload)
        self.assertRedirects(response, reverse("job_detail", args=[self.job.pk]) + "?step=review")
        url = reverse("source_page", args=[doc.pk, 1])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"\x89PNG"))
        self.assertEqual(self.client.get(reverse("source_page", args=[doc.pk, 2])).status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
