from io import BytesIO
from types import SimpleNamespace

from django.test import SimpleTestCase
from pypdf import PdfReader

from .extraction import get_schema
from .extraction.gemini import validate_fields
from .pdf_export import render_pdf


class CertificateRevisionTests(SimpleTestCase):
    def report(self, snapshot, approved_at=None):
        return SimpleNamespace(snapshot=snapshot, approved_at=approved_at, revision=1, pk="preview")

    def text(self, pdf):
        return "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)

    def test_cover_uses_source_value_with_unreviewed_marker(self):
        fields = [
            {
                "id": 1,
                "form_type": "transformer_proforma",
                "schema_key": "rated_power",
                "status": "unreviewed",
                "value": "250",
                "unit": "kVA",
                "source": "Proforma p. 1",
                "document_id": None,
                "page": 1,
            },
            {
                "id": 2,
                "form_type": "transformer_proforma",
                "schema_key": "serial_number",
                "status": "unreviewed",
                "value": "1098",
                "unit": "",
                "source": "Proforma p. 1",
                "document_id": None,
                "page": 1,
            },
        ]
        snapshot = {
            "fields": fields,
            "sections": [],
            "calculations": [],
            "customer": "Fixture customer",
            "scope": [],
            "documents": [],
        }
        text = self.text(render_pdf(self.report(snapshot)))
        self.assertIn("250 kVA [unreviewed]", text)
        self.assertIn("1098 [unreviewed]", text)
        headings = (
            "SUMMARY OF RESULTS",
            "SAMPLE DESCRIPTION",
            "ROUTINE TESTS",
            "NO-LOAD LOSS AND CURRENT",
            "LOAD LOSS, IMPEDANCE AND TOTAL LOSS",
            "TEMPERATURE RISE TEST",
            "SHORT-CIRCUIT WITHSTAND TEST",
            "PRESSURE AND OIL LEAKAGE TESTS",
            "CROSS-TEST CONSISTENCY",
            "OBSERVATIONS AND REMARKS",
            "ANNEXURES",
        )
        positions = [text.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))
        self.assertGreater(
            text.index("C. Source readings and transcription status"), text.index("ANNEXURES")
        )

    def test_provisional_preview_is_watermarked_and_never_issuable(self):
        snapshot = {
            "fields": [],
            "sections": [],
            "calculations": [],
            "provisional_preview": True,
            "documents": [],
        }
        text = self.text(render_pdf(self.report(snapshot)))
        self.assertIn("AUTOMATED DRAFT - NOT ISSUED", text)
        with self.assertRaisesRegex(ValueError, "cannot be issued"):
            render_pdf(self.report(snapshot, approved_at=True))

    def test_assumed_rule_result_is_labeled_provisional_in_draft(self):
        calculation = {
            "code": "TOP_OIL_RISE",
            "version": 1,
            "title": "Top Oil Rise",
            "test_type": "temperature_rise",
            "operation": "identity",
            "verdict": "pass",
            "value": "26.1",
            "unit": "K",
            "limit": "35",
            "margin": "8.9",
            "margin_unit": "K",
            "rule_status": "assumed",
            "source_clause": "Unconfirmed fixture clause",
            "inputs": [],
        }
        snapshot = {
            "fields": [],
            "sections": [],
            "calculations": [calculation],
            "documents": [],
            "scope": ["temperature_rise"],
        }
        content = self.text(render_pdf(self.report(snapshot)))
        self.assertIn("PROVISIONAL PASS", content)

    def test_schema_has_twelve_distinct_deflection_rows_and_source_hints(self):
        oil = get_schema("pressure_oil_leakage")
        points = [
            field
            for field in oil["fields"]
            if field["key"].startswith("deflection.") and field["key"].endswith(".point")
        ]
        self.assertEqual(len(points), 12)
        self.assertEqual(points[0]["label"], "pressure point 1 point")
        self.assertEqual(points[-1]["label"], "oil point 6 point")
        self.assertIn("applied_through", [field["key"] for field in oil["fields"]])
        rise = get_schema("temperature_rise")
        self.assertTrue(any("235" in instruction for instruction in rise["instructions"]))

    def test_struck_out_reading_is_preserved_as_cancelled_evidence(self):
        schema = {"fields": [{"key": "reading", "page": 1, "label": "Reading"}]}
        payload = {
            "fields": [
                {
                    "key": "reading",
                    "raw_text": "24.8",
                    "unit": "°C",
                    "status": "struck_out",
                    "box_2d": [10, 10, 30, 30],
                }
            ]
        }
        result = validate_fields(payload, schema, 1)
        self.assertEqual(result[0]["status"], "struck_out")
        self.assertEqual(result[0]["raw_text"], "24.8")
