import json
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from .assembly import assemble
from .extraction import get_provider
from .extraction.gemini import ExtractionError
from .extraction.local import LocalProvider, generate
from .insights import generate as generate_insights
from .models import InsightDraft, Job


@override_settings(EXTRACTION_PROVIDER="local_qwen", LOCAL_AI_TIMEOUT=30)
class LocalProviderTests(SimpleTestCase):
    def response(self, payload, truncated=False):
        return BytesIO(
            json.dumps(
                {
                    "text": json.dumps(payload),
                    "truncated": truncated,
                    "model": "fixture",
                    "revision": "abc",
                    "seconds": 1,
                    "tokens": 10,
                    "peak_gpu_mb": 0,
                }
            ).encode()
        )

    def test_provider_and_batched_source_evidence(self):
        fields = [{"key": f"f{i}", "page": 1} for i in range(11)]
        payload = {
            "fields": [
                {
                    "key": "f0",
                    "raw_text": "0.0030",
                    "unit": "ohm",
                    "status": "read",
                    "box_2d": [10, 20, 30, 40],
                }
            ]
        }
        with patch(
            "lab.extraction.local.urlopen",
            side_effect=[self.response(payload), self.response({"fields": []})],
        ) as request:
            result = get_provider().extract_page(
                b"source-image", {"form_type": "test", "fields": fields}, 1
            )
        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(result["fields"]), 11)
        self.assertEqual(result["fields"][0]["raw_text"], "0.0030")
        self.assertEqual(result["fields"][0]["bbox"], [0.02, 0.01, 0.04, 0.03])
        self.assertEqual(result["fields"][-1]["status"], "missing")
        self.assertEqual(result["metadata"]["provider"], "local_qwen")
        self.assertNotIn("0.0030", json.loads(request.call_args_list[0].args[0].data)["prompt"])

    def test_truncated_and_failed_inference_rejected(self):
        with patch(
            "lab.extraction.local.urlopen", return_value=self.response({"fields": []}, True)
        ):
            with self.assertRaisesRegex(ExtractionError, "output limit"):
                generate("test")
        with patch("lab.extraction.local.urlopen", side_effect=TimeoutError):
            with self.assertRaisesRegex(ExtractionError, "unavailable"):
                generate("test")

    def test_wrong_field_is_rejected(self):
        payload = {
            "fields": [
                {
                    "key": "unknown",
                    "raw_text": "2",
                    "unit": "",
                    "status": "read",
                    "box_2d": [0, 0, 1, 1],
                }
            ]
        }
        with patch("lab.extraction.local.urlopen", return_value=self.response(payload)):
            with self.assertRaises(ExtractionError):
                LocalProvider().extract_page(
                    b"image", {"fields": [{"key": "a", "page": 1}], "form_type": "test"}, 1
                )


@override_settings(INSIGHT_PROVIDER="local_qwen", GEMINI_API_KEY="")
class LocalInsightTests(TestCase):
    def test_local_insights_need_no_cloud_key_and_retain_evidence_gate(self):
        user = get_user_model().objects.create_user("local-ai-test")
        job = Job.objects.create(owner=user, title="Fixture", customer="Fixture")
        data = assemble(job)
        fact = data["findings"][0]
        draft = InsightDraft.objects.create(job=job, evidence_hash=data["evidence_hash"])
        payload = {
            "items": [
                {
                    "title": "Review scope",
                    "explanation": "The scope needs confirmation.",
                    "recommended_check": "Confirm requested tests.",
                    "finding_ids": [fact["id"]],
                }
            ]
        }
        with patch(
            "lab.extraction.local.generate",
            return_value=(payload, {"model": "fixture", "revision": "abc"}),
        ):
            generate_insights(draft.pk)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "ready")
        self.assertIsNone(draft.reviewed_by)
        self.assertEqual(draft.provider, "Local / fixture @ abc")
        draft.status = "queued"
        draft.save()
        payload["items"][0]["finding_ids"] = ["invented"]
        with patch(
            "lab.extraction.local.generate",
            return_value=(payload, {"model": "fixture", "revision": "abc"}),
        ):
            generate_insights(draft.pk)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "failed")
