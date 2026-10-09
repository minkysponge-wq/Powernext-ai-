"""A second same-schema form exercises import, human review, and PDF page fit."""

import json
import shutil
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase, override_settings
from pypdf import PdfReader


class UnseenFormStressTests(TestCase):
    def test_long_source_label_still_renders_eighteen_reviewed_pages(self):
        repository = Path(__file__).resolve().parents[2]
        fixture = repository / "demo" / "forms" / "SYN-UNSEEN-002-readings.csv"
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            forms = root / "demo" / "forms"
            forms.mkdir(parents=True)
            shutil.copyfile(fixture, forms / fixture.name)
            with override_settings(
                DEBUG=True,
                BASE_DIR=root / "app",
                MEDIA_ROOT=root / "private",
                MFA_ENFORCED=False,
            ):
                call_command("stress_unseen_report", iterations=1, stdout=StringIO())
            output = root / "output" / "stress-unseen"
            result = json.loads((output / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["form_rows"], 999)
            self.assertTrue(result["imported_as_unreviewed"])
            self.assertEqual(result["template_pending_after_review"], 0)
            self.assertEqual(result["page_count"], 18)
            self.assertTrue(result["draft_only"])
            pdf = PdfReader(output / "SYN-UNSEEN-002-DRAFT.pdf")
            self.assertEqual(len(pdf.pages), 18)
            self.assertIn("No additional engineer remark recorded.", pdf.pages[12].extract_text())
