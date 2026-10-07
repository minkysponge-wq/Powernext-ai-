"""Numerical examples relayed by the team; not independent source transcription."""

from io import BytesIO
from types import SimpleNamespace

from django.test import SimpleTestCase
from pypdf import PdfReader

from .calculations import evaluate_rule
from .pdf_export import render_pdf


class VerdictRuleTests(SimpleTestCase):
    def test_report_analysis_uses_frozen_verdicts_and_reviewed_evidence_only(self):
        from .report_analysis import analysis

        marginal = {
            "code": "HV_WINDING_RISE",
            "version": 1,
            "title": "HV winding rise",
            "test_type": "temperature_rise",
            "operation": "identity",
            "verdict": "marginal",
            "value": "39.8",
            "unit": "K",
            "limit_side": "upper",
            "limit": "40",
            "margin": "0.2",
            "margin_unit": "K",
            "rule_status": "confirmed",
            "source_clause": "Fixture clause",
            "inputs": [{"value": "39.8", "unit": "K", "source": "Station A"}],
        }
        snapshot = {
            "calculations": [marginal],
            "verdict_summary": {"marginal": 1},
            "fields": [
                {
                    "form_type": "temperature_rise",
                    "schema_key": "hv_winding_rise",
                    "status": "verified",
                    "value": "39.8",
                    "unit": "K",
                    "source": "Station A",
                },
                {
                    "form_type": "temperature_rise",
                    "schema_key": "top_oil_rise",
                    "status": "unreviewed",
                    "value": "9999",
                    "unit": "K",
                    "source": "Scan",
                },
            ],
        }
        result = analysis(snapshot)
        lines = " ".join(row["text"] for rows in result["sections"].values() for row in rows)
        self.assertIn("marginal; measured 39.8 K", lines)
        self.assertIn("signed margin 0.2 K", lines)
        self.assertNotIn("9999", lines)
        self.assertIn("Fixture clause", str(result["sections"]["temperature_rise"]))

    def rule(self, operation, parameters):
        return SimpleNamespace(
            code="EXAMPLE",
            version=1,
            title="Example engineering check",
            operation=operation,
            parameters=parameters,
            source_clause="Team-relayed organiser example; clause needs confirmation",
            status="assumed",
        )

    def field(self, key, value, unit, status="verified", form_type="example"):
        return {
            "key": key,
            "schema_key": key,
            "form_type": form_type,
            "value": str(value),
            "unit": unit,
            "status": status,
            "id": 1,
        }

    def test_team_relayed_numerical_examples(self):
        cases = [
            (
                "loss 50",
                "identity",
                ["actual"],
                "W",
                {"upper": 980, "marginal_percent": 2},
                [self.field("actual", 918, "W")],
                "pass",
                "62",
            ),
            (
                "loss 100",
                "identity",
                ["actual"],
                "W",
                {"upper": 2930, "marginal_percent": 2},
                [self.field("actual", 2474, "W")],
                "pass",
                "456",
            ),
            (
                "winding rise",
                "identity",
                ["actual"],
                "K",
                {"upper": 40, "marginal_percent": 2},
                [self.field("actual", 39.8, "K")],
                "marginal",
                "0.2",
            ),
            (
                "reactance change",
                "absolute_percent_change",
                ["before", "after"],
                "%",
                {"upper": 2, "marginal_percent": 2},
                [self.field("before", 100, "%"), self.field("after", 100.204, "%")],
                "pass",
                "1.79600",
            ),
            (
                "ratio error",
                "percent_deviation",
                ["measured", "nominal"],
                "",
                {"lower": -0.5, "upper": 0.5, "marginal_percent": 2},
                [self.field("measured", 1.002, ""), self.field("nominal", 1, "")],
                "pass",
                "0.300",
            ),
            (
                "pressure boundary",
                "identity",
                ["actual"],
                "mm",
                {"upper": 2, "marginal_percent": 2},
                [self.field("actual", 2, "mm")],
                "marginal",
                "0",
            ),
        ]
        for label, operation, keys, unit, limits, fields, verdict, margin in cases:
            with self.subTest(label=label):
                result = evaluate_rule(
                    self.rule(operation, {"fields": keys, "unit": unit, **limits}), fields
                )
                self.assertEqual(result["verdict"], verdict)
                self.assertEqual(result["margin"], margin)
                self.assertEqual(result["rule_status"], "assumed")

    def test_missing_unreviewed_duplicate_or_bad_unit_never_passes(self):
        rule = self.rule(
            "identity",
            {
                "inputs": [{"form_type": "temperature_rise", "schema_key": "top_oil_rise"}],
                "unit": "K",
                "upper": 35,
                "marginal_percent": 2,
            },
        )
        good = self.field("top_oil_rise", 25.8, "K", form_type="temperature_rise")
        self.assertEqual(evaluate_rule(rule, [good])["verdict"], "pass")
        for fields in (
            [],
            [dict(good, status="unreviewed")],
            [dict(good, unit="C")],
            [good, dict(good, id=2)],
        ):
            self.assertEqual(evaluate_rule(rule, fields)["verdict"], "blocked")

    def test_reviewed_derived_loss_uses_recorded_limit(self):
        calculations = [
            {
                "form_type": "loss_calculation",
                "source": "Reviewed worksheet",
                "document_id": "doc-1",
                "rows": [{"stage": "NTBT", "total_loss_100": 2474, "limit_100": 2930}],
                "error": "",
            }
        ]
        inputs = [
            {"derived": {"form_type": "loss_calculation", "stage": "NTBT", "metric": metric}}
            for metric in ("total_loss_100", "limit_100")
        ]
        rule = self.rule(
            "identity", {"inputs": inputs, "unit": "W", "upper_input": 1, "marginal_percent": 2}
        )
        result = evaluate_rule(rule, [], calculations)
        self.assertEqual((result["verdict"], result["margin"]), ("pass", "456"))
        self.assertEqual(evaluate_rule(rule, [], [])["verdict"], "blocked")
        self.assertEqual(evaluate_rule(rule, [], calculations * 2)["verdict"], "blocked")

    def test_categorical_rule_requires_explicit_accepted_observations(self):
        rule = self.rule(
            "all_text",
            {
                "fields": ["visual", "routine"],
                "unit": "",
                "accepted_values": ["Satisfactory", "Withstood"],
                "rejected_values": ["breakdown observed"],
            },
        )
        fields = [self.field("visual", "Satisfactory", ""), self.field("routine", "Withstood", "")]
        self.assertEqual(evaluate_rule(rule, fields)["verdict"], "pass")
        fields[1]["value"] = "No disruptive discharge; withstood"
        self.assertEqual(evaluate_rule(rule, fields)["verdict"], "blocked")
        fields[1]["value"] = "breakdown observed"
        self.assertEqual(evaluate_rule(rule, fields)["verdict"], "fail")

    def test_pdf_places_verdict_summary_before_detail_table(self):
        verdict = evaluate_rule(
            self.rule(
                "identity",
                {
                    "fields": ["rise"],
                    "unit": "K",
                    "upper": 40,
                    "marginal_percent": 2,
                    "test_type": "temperature_rise",
                },
            ),
            [self.field("rise", 39.8, "K")],
        )
        snapshot = {
            "title": "Synthetic test",
            "customer": "Example customer",
            "test_series": "DEMO",
            "sample_code": "SYN-001",
            "scope_note": "Synthetic scope",
            "blockers": 1,
            "calculations": [verdict],
            "verdict_summary": {"pass": 0, "marginal": 1, "fail": 0, "blocked": 0},
            "findings": [],
            "fields": [],
            "sections": [],
            "limitations": "Synthetic test only.",
        }
        report = SimpleNamespace(
            snapshot=snapshot,
            approved_at=None,
            revision=1,
            pk="synthetic-report",
            snapshot_sha256="0" * 64,
        )
        pdf = PdfReader(BytesIO(render_pdf(report)))
        text = " ".join(" ".join(page.extract_text().split()) for page in pdf.pages)
        self.assertIn("DRAFT - REVIEW REQUIRED", pdf.pages[0].extract_text())
        self.assertLess(text.index("SUMMARY OF RESULTS"), text.index("Example engineering check"))

    def test_pdf_starts_test_on_new_page_without_database_field_ids(self):
        measured = dict(
            self.field("top_oil_rise", 25.8, "K", form_type="temperature_rise"),
            id=987654,
            label="Top-oil rise",
            source="Temperature station",
            page=1,
            document_id=None,
            schema_page=1,
        )
        guarantee = dict(
            self.field("guaranteed_temp_rise_1", 35, "K", form_type="transformer_proforma"),
            id=987655,
            label="Guaranteed top-oil rise",
        )
        snapshot = {
            "title": "Synthetic test",
            "customer": "Example customer",
            "test_series": "DEMO",
            "sample_code": "SYN-001",
            "file_number": "SYN-FILE-001",
            "scope_note": "Synthetic scope",
            "blockers": 0,
            "calculations": [],
            "findings": [],
            "fields": [measured, guarantee],
            "report_sections": [
                {
                    "key": "temperature_rise",
                    "title": "Temperature-rise test",
                    "fields": [measured],
                    "missing": [],
                    "applicable": True,
                }
            ],
            "limitations": "Synthetic test only.",
        }
        report = SimpleNamespace(
            snapshot=snapshot,
            approved_at=None,
            revision=1,
            pk="internal-report-id",
            snapshot_sha256="0" * 64,
        )
        pages = PdfReader(BytesIO(render_pdf(report))).pages
        self.assertGreaterEqual(len(pages), 2)
        self.assertNotIn("Temperature-rise test", pages[0].extract_text())
        text = "\n".join(page.extract_text() for page in pages)
        self.assertIn("Measured rise (dark) and recorded guarantee (light)", text)
        self.assertIn("SYN-FILE-001", text)
        self.assertNotIn("987654", text)
        self.assertNotIn("internal-report-id", text)
