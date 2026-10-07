import csv
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from pypdf import PdfReader

from .assembly import assemble
from .extraction import get_schema
from .insights import generate, validate
from .models import Field, InsightDraft, Job, Rule
from .report_workflow import issue_code, job_locked, verify_issue_seal
from .structured_import import import_csv


class ReportingTests(TestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        setting = override_settings(MEDIA_ROOT=self.storage.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.user = get_user_model().objects.create_user("report-engineer")
        self.job = Job.objects.create(
            owner=self.user,
            title="Synthetic acceptance case",
            customer="Fixture customer",
            sample_code="S1",
            test_series="T1",
        )
        self.client.force_login(self.user)

    def upload(self, rows):
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["form_type", "key", "value", "unit"])
        writer.writerows(rows)
        return SimpleUploadedFile("readings.csv", stream.getvalue().encode())

    def complete_instruction(self):
        self.job.report_scope = ["work_instruction", "pressure_oil_leakage"]
        self.job.scope_note = (
            "Synthetic pressure-test acceptance case; other tests excluded for this fixture."
        )
        self.job.save()
        for kind in self.job.report_scope:
            for f in get_schema(kind)["fields"]:
                value = {
                    "sample_code": "S1",
                    "sample_pressure": "S1",
                    "test_series": "T1",
                    "series": "T1",
                    "customer": "Fixture customer",
                    "oil_observation": "No leakage observed - synthetic fixture",
                }.get(f["key"], "")
                Field.objects.create(
                    job=self.job,
                    key=f"digital.{kind}.p{f['page']}.{f['key']}",
                    label=f["label"],
                    value=value,
                    raw_value=value,
                    origin="digital",
                    status="verified" if value else "not_applicable",
                    updated_by=self.user,
                    context={
                        "form_type": kind,
                        "schema_page": f["page"],
                        "schema_key": f["key"],
                        "source_reference": "Synthetic fixture",
                    },
                )

    def make_report(self):
        self.client.post(reverse("make_report", args=[self.job.pk]))
        return self.job.reports.first()

    def test_csv_import_duplicate_and_atomic_invalid_file(self):
        rows = [
            ["work_instruction", "sample_code", "S1", ""],
            ["work_instruction", "test_series", "T1", ""],
        ]
        self.assertEqual(import_csv(self.job, self.upload(rows), "Export 1", self.user), 2)
        self.assertEqual(import_csv(self.job, self.upload(rows), "Export 1", self.user), 0)
        self.assertTrue(all(f.status == "unreviewed" for f in self.job.fields.all()))
        with self.assertRaises(ValueError):
            import_csv(
                self.job,
                self.upload([["work_instruction", "customer", "C", ""], ["bad", "key", "x", ""]]),
                "Bad",
                self.user,
            )
        self.assertEqual(self.job.fields.count(), 2)
        with self.assertRaises(ValueError):
            import_csv(
                self.job,
                self.upload([["work_instruction", "sample_code", "S2", ""]]),
                "Changed",
                self.user,
            )
        self.assertEqual(self.job.fields.get(context__schema_key="sample_code").value, "S1")

    def test_identity_conflicts_block_approval(self):
        self.complete_instruction()
        self.assertEqual(assemble(self.job)["blockers"], 0)
        self.job.fields.filter(context__schema_key="sample_code").update(value="OTHER")
        self.assertTrue(
            any(f["id"] == "identity-sample-code" for f in assemble(self.job)["findings"])
        )

    def test_unrecognised_text_verdict_blocks_engineer_lock(self):
        self.complete_instruction()
        rule = Rule.objects.create(
            code="FIXTURE_TEXT",
            title="Fixture observation",
            version=1,
            operation="all_text",
            status="confirmed",
            source_clause="Synthetic fixture clause",
            parameters={
                "inputs": [{"form_type": "pressure_oil_leakage", "schema_key": "oil_observation"}],
                "unit": "",
                "accepted_values": ["No leakage"],
            },
        )
        self.job.rules.add(rule)
        snapshot = assemble(self.job)
        self.assertEqual(snapshot["calculations"][0]["verdict"], "blocked")
        self.assertGreater(snapshot["blockers"], 0)
        report = self.make_report()
        self.user.user_permissions.add(Permission.objects.get(codename="lock_report"))
        self.assertEqual(
            self.client.post(
                reverse("engineer_lock", args=[report.pk]), {"confirmed": "yes"}
            ).status_code,
            400,
        )

    def test_rule_without_limit_blocks_engineer_lock(self):
        self.complete_instruction()
        field = self.job.fields.get(context__schema_key="routine_pressure")
        field.value = "20"
        field.unit = "kPa"
        field.status = "verified"
        field.save()
        rule = Rule.objects.create(
            code="FIXTURE_UNBOUNDED",
            title="Fixture pressure",
            version=1,
            operation="identity",
            status="confirmed",
            source_clause="Synthetic fixture clause",
            parameters={
                "inputs": [{"form_type": "pressure_oil_leakage", "schema_key": "routine_pressure"}],
                "unit": "kPa",
            },
        )
        self.job.rules.add(rule)
        snapshot = assemble(self.job)
        self.assertEqual(snapshot["calculations"][0]["verdict"], "not_configured")
        self.assertGreater(snapshot["blockers"], 0)
        self.assertIn(
            "overall conformity is withheld", snapshot["report_analysis"]["overall"]["text"]
        )
        report = self.make_report()
        self.user.user_permissions.add(Permission.objects.get(codename="lock_report"))
        self.assertEqual(
            self.client.post(
                reverse("engineer_lock", args=[report.pk]), {"confirmed": "yes"}
            ).status_code,
            400,
        )

    def test_provisional_pdf_is_private_and_does_not_change_frozen_draft(self):
        report = self.make_report()
        frozen = report.snapshot
        url = reverse("report_pdf", args=[report.pk]) + "?provisional=1"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        text = "\n".join(
            page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages
        )
        self.assertIn("AUTOMATED DRAFT - NOT ISSUED", text)
        report.refresh_from_db()
        self.assertEqual(report.snapshot, frozen)
        outsider = get_user_model().objects.create_user("pdf-outsider")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_sections_pdf_archive_and_approval_lifecycle(self):
        self.complete_instruction()
        report = self.make_report()
        self.assertEqual(report.snapshot["sections"][0]["key"], "work_instruction")
        url = reverse("approve_report", args=[report.pk])
        self.assertEqual(self.client.post(url, {"confirmed": "yes"}).status_code, 403)
        self.user.user_permissions.add(Permission.objects.get(codename="lock_report"))
        self.assertEqual(
            self.client.post(
                reverse("engineer_lock", args=[report.pk]), {"confirmed": "yes"}
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(
                reverse("edit_field", args=[self.job.pk, self.job.fields.first().pk]),
                {"value": "changed"},
            ).status_code,
            409,
        )
        quality = get_user_model().objects.create_user("quality-reviewer")
        quality.user_permissions.add(Permission.objects.get(codename="verify_report"))
        self.client.force_login(quality)
        self.assertEqual(
            self.client.post(
                reverse("quality_verify", args=[report.pk]), {"confirmed": "yes"}
            ).status_code,
            302,
        )
        hod = get_user_model().objects.create_user("hod-signer")
        hod.user_permissions.add(Permission.objects.get(codename="approve_report"))
        self.client.force_login(hod)
        self.assertEqual(self.client.post(url, {"confirmed": "yes"}).status_code, 302)
        report.refresh_from_db()
        self.assertIsNotNone(report.approved_at)
        self.assertTrue(verify_issue_seal(report))
        verification = reverse("verify_issued", args=[report.pk, issue_code(report)])
        self.assertContains(self.client.get(verification), "Application seal valid")
        self.assertNotIn("Fixture customer", self.client.get(verification).content.decode())
        self.assertEqual(
            self.client.get(reverse("verify_issued", args=[report.pk, "0" * 24])).status_code, 404
        )
        sealed_hash = report.snapshot_sha256
        report.snapshot_sha256 = "0" * 64
        self.assertFalse(verify_issue_seal(report))
        report.snapshot_sha256 = sealed_hash
        response = self.client.get(reverse("report_pdf", args=[report.pk]))
        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
        preview = self.client.get(reverse("report_pdf", args=[report.pk]) + "?preview=1")
        self.assertTrue(preview["Content-Disposition"].startswith("inline;"))
        self.assertEqual(preview["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'", preview["Content-Security-Policy"])
        preview.close()
        pdf = b"".join(response.streaming_content)
        text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
        self.assertIn("ISSUED CERTIFICATE", text)
        self.assertIn("SUMMARY OF RESULTS", text)
        self.assertIn("CROSS-TEST CONSISTENCY", text)
        self.assertIn("Source readings and transcription status", text)
        self.assertContains(
            self.client.get(reverse("report_archive") + "?q=S1"), "Synthetic acceptance case"
        )
        frozen = report.snapshot
        self.job.fields.filter(context__schema_key="customer").update(value="Later value")
        report.refresh_from_db()
        self.assertEqual(report.snapshot, frozen)
        with patch(
            "lab.pdf_export.render_pdf",
            side_effect=AssertionError("Approved PDF must not regenerate"),
        ):
            exported = self.client.get(reverse("report_pdf", args=[report.pk]))
            self.assertEqual(b"".join(exported.streaming_content), pdf)

    def test_unissued_report_cannot_be_publicly_verified(self):
        self.complete_instruction()
        report = self.make_report()
        self.assertEqual(
            self.client.get(reverse("verify_issued", args=[report.pk, "0" * 24])).status_code, 404
        )

    def test_stale_report_and_incomplete_report_cannot_be_approved(self):
        self.user.user_permissions.add(Permission.objects.get(codename="approve_report"))
        report = self.make_report()
        self.assertEqual(
            self.client.post(
                reverse("approve_report", args=[report.pk]), {"confirmed": "yes"}
            ).status_code,
            409,
        )
        self.user.user_permissions.add(Permission.objects.get(codename="lock_report"))
        self.assertEqual(
            self.client.post(
                reverse("engineer_lock", args=[report.pk]), {"confirmed": "yes"}
            ).status_code,
            400,
        )
        self.complete_instruction()
        report = self.make_report()
        self.job.fields.filter(context__schema_key="customer").update(value="Changed")
        self.assertEqual(
            self.client.post(
                reverse("engineer_lock", args=[report.pk]), {"confirmed": "yes"}
            ).status_code,
            409,
        )

    def test_distinct_reviewers_and_return_for_correction(self):
        self.complete_instruction()
        report = self.make_report()
        self.user.user_permissions.add(Permission.objects.get(codename="lock_report"))
        self.user.user_permissions.add(Permission.objects.get(codename="verify_report"))
        self.assertEqual(
            self.client.post(
                reverse("engineer_lock", args=[report.pk]), {"confirmed": "yes"}
            ).status_code,
            302,
        )
        self.assertTrue(job_locked(self.job))
        self.assertEqual(
            self.client.post(
                reverse("quality_verify", args=[report.pk]), {"confirmed": "yes"}
            ).status_code,
            403,
        )
        quality = get_user_model().objects.create_user("separate-quality")
        quality.user_permissions.add(Permission.objects.get(codename="verify_report"))
        self.client.force_login(quality)
        self.assertEqual(
            self.client.post(
                reverse("return_for_correction", args=[report.pk]), {"reason": ""}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                reverse("return_for_correction", args=[report.pk]),
                {"reason": "Check customer reference against request"},
            ).status_code,
            302,
        )
        report.refresh_from_db()
        self.assertIsNotNone(report.returned_at)
        self.assertFalse(job_locked(self.job))
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.post(
                reverse("engineer_lock", args=[report.pk]), {"confirmed": "yes"}
            ).status_code,
            409,
        )
        self.job.fields.filter(context__schema_key="customer").update(value="Corrected customer")
        self.assertEqual(
            self.client.post(reverse("make_report", args=[self.job.pk])).status_code, 302
        )
        self.assertEqual(self.job.reports.count(), 2)

    def test_insight_validation_rejects_unsupported_evidence(self):
        valid = {
            "items": [
                {
                    "title": "Check sample",
                    "explanation": "Sources disagree.",
                    "recommended_check": "Compare identifiers.",
                    "finding_ids": ["identity"],
                }
            ]
        }
        self.assertEqual(len(validate(valid, {"identity"})), 1)
        with self.assertRaises(ValueError):
            validate(valid, {"other"})
        with self.assertRaises(ValueError):
            validate({"items": []}, set())

    @override_settings(CLOUD_AI_ALLOWED=True, PAID_AI_ALLOWED=True)
    def test_ai_failure_does_not_block_rule_findings_or_reporting(self):
        from urllib.error import HTTPError

        draft = InsightDraft.objects.create(
            job=self.job, evidence_hash=assemble(self.job)["evidence_hash"]
        )
        with self.settings(GEMINI_API_KEY="test-not-a-secret", GEMINI_MODEL="fixture-model"):
            with patch(
                "lab.insights.urlopen", side_effect=HTTPError("provider", 429, "quota", {}, None)
            ):
                generate(draft.pk)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "failed")
        self.assertIn("429", draft.message)
        self.assertContains(
            self.client.get(reverse("findings", args=[self.job.pk])), "Confirm the requested tests"
        )
        self.assertIsNotNone(self.make_report())

    @override_settings(CLOUD_AI_ALLOWED=True, PAID_AI_ALLOWED=False, INSIGHT_PROVIDER="gemini")
    def test_paid_insight_switch_prevents_network_request(self):
        draft = InsightDraft.objects.create(
            job=self.job, evidence_hash=assemble(self.job)["evidence_hash"]
        )
        with patch("lab.insights.urlopen") as outbound:
            generate(draft.pk)
        outbound.assert_not_called()
        draft.refresh_from_db()
        self.assertEqual(draft.status, "failed")

    def test_reviewed_ai_only_and_stale_explanation_excluded(self):
        self.complete_instruction()
        draft = InsightDraft.objects.create(
            job=self.job,
            evidence_hash=assemble(self.job)["evidence_hash"],
            status="ready",
            content=[
                {
                    "title": "Fixture",
                    "explanation": "Synthetic narrative",
                    "recommended_check": "Review evidence",
                    "finding_ids": [],
                }
            ],
        )
        self.assertEqual(self.make_report().snapshot["ai_insights"], [])
        self.assertEqual(
            self.client.post(
                reverse("approve_insights", args=[draft.pk]), {"confirmed": "yes"}
            ).status_code,
            302,
        )
        self.assertEqual(len(self.make_report().snapshot["ai_insights"]), 1)
        self.job.fields.filter(context__schema_key="customer").update(value="Changed")
        self.assertEqual(self.make_report().snapshot["ai_insights"], [])

    def test_private_reporting_routes(self):
        other = get_user_model().objects.create_user("outsider")
        self.client.force_login(other)
        for name in [
            "findings",
            "job_scope",
            "import_readings",
            "import_template",
            "digital_review",
        ]:
            self.assertEqual(self.client.get(reverse(name, args=[self.job.pk])).status_code, 404)

    def test_digital_worksheet_calculations_reach_report(self):
        import re

        values = json.loads(
            Path(__file__)
            .with_name("fixtures")
            .joinpath("synthetic_datasheet_v217.json")
            .read_text()
        )["values"]
        for key, value in values.items():
            unit = ""
            if key == "rated_power":
                unit = "kVA"
            elif key in ("rated_hv", "rated_lv") or re.search(r"\.V[123]?$", key):
                unit = "V"
            elif key.startswith("guarantee_") or re.search(r"\.P[123]?$", key):
                unit = "W"
            elif key.startswith("hv_resistance.") and ".R" in key:
                unit = "Ohm"
            elif key.startswith("lv_resistance.") and ".R" in key:
                unit = "mOhm"
            elif key.endswith(".temperature"):
                unit = "degC"
            elif re.search(r"\.I[123]?$", key):
                unit = "A"
            elif key.endswith(".f"):
                unit = "Hz"
            Field.objects.create(
                job=self.job,
                key=key,
                label=key,
                value=str(value),
                unit=unit,
                origin="digital",
                status="verified",
                updated_by=self.user,
                context={"form_type": "loss_calculation", "schema_key": key},
            )
        data = assemble(self.job)
        self.assertEqual(len(data["transformer_calculations"][0]["rows"]), 6)
        self.assertTrue(any("total loss" in f["title"] for f in data["findings"]))
        self.assertEqual(len(self.make_report().snapshot["transformer_calculations"][0]["rows"]), 6)

    def test_all_new_screens_render(self):
        for name in ["findings", "job_scope", "import_readings", "digital_review"]:
            self.assertEqual(self.client.get(reverse(name, args=[self.job.pk])).status_code, 200)

    def test_generated_template_roundtrips_multipage_fields(self):
        response = self.client.get(reverse("import_template", args=[self.job.pk]))
        count = import_csv(
            self.job,
            SimpleUploadedFile("template.csv", response.content),
            "Template roundtrip",
            self.user,
        )
        self.assertGreater(count, 900)
        self.assertEqual(
            self.job.fields.filter(
                context__form_type="transformer_proforma", context__schema_key="sample_code"
            ).count(),
            2,
        )

    def test_legacy_multipage_reference_key_maps_to_schema(self):
        field = Field.objects.create(
            job=self.job,
            key="customer_request.p2.conformity_requested",
            label="Conformity requested",
            value="Yes",
            origin="digital",
            updated_by=self.user,
            context={"form_type": "customer_request", "schema_page": 2},
        )
        item = next(f for f in assemble(self.job)["fields"] if f["id"] == field.pk)
        self.assertEqual(item["schema_key"], "conformity_requested")
