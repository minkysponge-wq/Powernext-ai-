"""Issued QR links must point to a real local or configured public host."""

import hashlib
from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from .report_workflow import uploaded_pdf_matches_issue
from .verification_link import report_verification_url


class VerificationLinkTests(SimpleTestCase):
    @override_settings(PUBLIC_VERIFY_BASE_URL="http://127.0.0.1:8000")
    def test_local_demo_link_is_openable(self):
        report = SimpleNamespace(pk="abc")
        self.assertEqual(
            report_verification_url(report, "token"),
            "http://127.0.0.1:8000/verify/abc/token/",
        )

    @override_settings(PUBLIC_VERIFY_BASE_URL="https://vectorlab.example.org")
    def test_placeholder_host_cannot_be_issued(self):
        with self.assertRaisesRegex(ValueError, "real VECTORLAB_PUBLIC_BASE_URL"):
            report_verification_url(SimpleNamespace(pk="abc"), "token")

    def test_tampered_pdf_upload_fails_hash_check(self):
        issued = b"%PDF-1.7\nsynthetic signed bytes"
        report = SimpleNamespace(approved_pdf_sha256=hashlib.sha256(issued).hexdigest())
        self.assertTrue(
            uploaded_pdf_matches_issue(report, SimpleUploadedFile("issued.pdf", issued))
        )
        self.assertFalse(
            uploaded_pdf_matches_issue(
                report, SimpleUploadedFile("tampered.pdf", issued.replace(b"signed", b"alterd"))
            )
        )
