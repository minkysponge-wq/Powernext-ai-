from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .assembly import assemble
from .insights import generate
from .models import Field, InsightDraft, Job


@override_settings(INSIGHT_PROVIDER="local_qwen", AI_JOB_TIMEOUT=1)
class InsightRecoveryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("insight-worker-fixture")
        self.job = Job.objects.create(owner=self.user, title="Insight fixture", customer="Fixture")
        self.client.force_login(self.user)

    def facts(self):
        return {
            "evidence_hash": "fixture-hash",
            "findings": [
                {
                    "id": "f1",
                    "severity": "warning",
                    "title": "Review value",
                    "detail": "Source differs",
                    "action": "Check scan",
                }
            ],
        }

    def answer(self):
        return (
            {
                "items": [
                    {
                        "title": "Review",
                        "explanation": "A value needs checking.",
                        "recommended_check": "Compare the original source.",
                        "finding_ids": ["f1"],
                    }
                ]
            },
            {"model": "fixture", "revision": "v1"},
        )

    def test_crashed_worker_claim_can_be_recovered(self):
        draft = InsightDraft.objects.create(
            job=self.job,
            evidence_hash="fixture-hash",
            status="processing",
            processing_started_at=timezone.now() - timedelta(seconds=120),
            processing_token="old",
        )
        with (
            patch("lab.insights.assemble", return_value=self.facts()),
            patch("lab.extraction.local.generate", return_value=self.answer()) as provider,
        ):
            generate(draft.pk)
        provider.assert_called_once()
        draft.refresh_from_db()
        self.assertEqual(draft.status, "ready")
        self.assertNotEqual(draft.processing_token, "old")

    def test_recent_claim_is_not_processed_twice(self):
        draft = InsightDraft.objects.create(
            job=self.job,
            evidence_hash="fixture-hash",
            status="processing",
            processing_started_at=timezone.now(),
            processing_token="active",
        )
        with patch("lab.extraction.local.generate") as provider:
            generate(draft.pk)
        provider.assert_not_called()
        draft.refresh_from_db()
        self.assertEqual(draft.processing_token, "active")

    def test_old_worker_cannot_overwrite_new_claim(self):
        draft = InsightDraft.objects.create(
            job=self.job,
            evidence_hash="fixture-hash",
            status="processing",
            processing_started_at=timezone.now() - timedelta(seconds=120),
            processing_token="old",
        )

        def supersede(*args, **kwargs):
            InsightDraft.objects.filter(pk=draft.pk).update(processing_token="new")
            return self.answer()

        with (
            patch("lab.insights.assemble", return_value=self.facts()),
            patch("lab.extraction.local.generate", side_effect=supersede),
        ):
            generate(draft.pk)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "processing")
        self.assertEqual(draft.processing_token, "new")

    def test_repeated_request_does_not_queue_duplicate_processing(self):
        Field.objects.create(
            job=self.job, key="fixture", label="Fixture", value="1", updated_by=self.user
        )
        evidence_hash = assemble(self.job)["evidence_hash"]
        InsightDraft.objects.create(
            job=self.job,
            evidence_hash=evidence_hash,
            status="processing",
            processing_started_at=timezone.now(),
            processing_token="active",
        )
        with patch("lab.reporting_views.async_task") as queue:
            for _ in range(2):
                response = self.client.post(reverse("request_insights", args=[self.job.pk]))
                self.assertEqual(response.status_code, 302)
        queue.assert_not_called()
        self.assertEqual(self.job.insight_drafts.count(), 1)

    def test_stale_request_can_be_requeued_once(self):
        Field.objects.create(
            job=self.job, key="fixture", label="Fixture", value="1", updated_by=self.user
        )
        draft = InsightDraft.objects.create(
            job=self.job,
            evidence_hash=assemble(self.job)["evidence_hash"],
            status="processing",
            processing_started_at=timezone.now() - timedelta(seconds=120),
            processing_token="old",
        )
        with patch("lab.reporting_views.async_task") as queue:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse("request_insights", args=[self.job.pk]))
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse("request_insights", args=[self.job.pk]))
        queue.assert_called_once_with("lab.insights.generate", draft.pk)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "queued")
        self.assertEqual(draft.processing_token, "")

    @override_settings(INSIGHT_PROVIDER="gemini", PAID_AI_ALLOWED=False, GEMINI_API_KEY="test-only")
    def test_budget_pause_does_not_create_or_queue_ai_insight(self):
        with patch("lab.reporting_views.async_task") as queue:
            response = self.client.post(reverse("request_insights", args=[self.job.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.job.insight_drafts.exists())
        queue.assert_not_called()
