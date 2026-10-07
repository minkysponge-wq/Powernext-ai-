from types import SimpleNamespace

from django.test import SimpleTestCase

from .pdf_export import render_pdf
from .pdf_text_check import verify_request_pdf_text


class IssuedPdfTextTests(SimpleTestCase):
    def test_customer_request_text_survives_rendering_exactly(self):
        request = {
            "customer": "M/s Example Industries, Ltd.",
            "customer_address": "12 Test Road, Chennai",
            "manufacturer": "ABC Power & Co.",
            "sample_particulars": "Transformer serial X-01",
        }
        snapshot = {
            "title": "Synthetic certificate",
            "report_title": "Test report",
            "file_number": "OV-DEMO-001",
            "test_series": "DEMO",
            "sample_code": "X-01",
            "scope_note": "Synthetic",
            "blockers": 0,
            "calculations": [],
            "findings": [],
            "fields": [],
            "sections": [],
            **request,
        }
        report = SimpleNamespace(
            snapshot=snapshot,
            revision=1,
            approved_at=None,
            pk="synthetic",
            snapshot_sha256="0" * 64,
        )
        pdf = render_pdf(report)
        self.assertEqual(verify_request_pdf_text(pdf, request), [])
        self.assertEqual(
            verify_request_pdf_text(pdf, {**request, "manufacturer": "ABC Power & Co,"}),
            ["manufacturer"],
        )
        self.assertEqual(
            verify_request_pdf_text(pdf, {**request, "customer": "M/s Example Industries Ltd."}),
            ["customer"],
        )
