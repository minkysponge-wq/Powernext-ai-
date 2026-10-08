from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from pypdf import PdfReader

from .fixed_report import _number, compile_fixed, render_fixed_html, render_fixed_pdf
from .fixed_template import default_fixed_definition, validate_fixed_definition
from .models import Job, ReportTemplate


class FixedTemplateTests(SimpleTestCase):
    def test_whole_number_limits_keep_significant_zeroes(self):
        self.assertEqual(_number("40"), "40")
        self.assertEqual(_number("250"), "250")
        self.assertEqual(_number("40.00"), "40")
        self.assertEqual(_number("120", "W"), "120.00")
        self.assertEqual(_number("40", "K"), "40.0")
        self.assertEqual(_number("4.299311815780109", "%"), "4.299")
        self.assertEqual(_number("0.01234567", "Ω"), "0.0123")

    def report(self, requested):
        definition = default_fixed_definition()
        snapshot = {
            "template_mapping": {"name": "CPRI-SCL-TR-v1", "version": 1, "definition": definition},
            "fields": [
                {
                    "form_type": "customer_request",
                    "schema_key": "customer_name",
                    "value": "A.P.TRANSFORMERS",
                    "status": "verified",
                    "unit": "",
                    "source": "CRF page 1",
                }
            ],
            "calculations": [],
            "transformer_calculations": [],
            "documents": [],
            "sample_code": "SYN-001",
            "test_series": "SYN-T1",
            "requested_test_ids": requested,
        }
        return SimpleNamespace(snapshot=snapshot, revision=1, approved_at=None)

    def test_linter_rejects_unbound_placeholder_and_unknown_field(self):
        definition = default_fixed_definition()
        self.assertTrue(validate_fixed_definition(definition))
        broken = deepcopy(definition)
        broken["pages"][0]["blocks"][1]["text"] = "{{not_mapped}}"
        with self.assertRaises(ValidationError):
            validate_fixed_definition(broken)
        broken = deepcopy(definition)
        broken["bindings"]["witness_name"]["key"] = "imaginary_field"
        with self.assertRaises(ValidationError):
            validate_fixed_definition(broken)

    def test_customer_text_exact_copy_required(self):
        definition = default_fixed_definition()
        definition["bindings"]["customer_name"]["exact_copy"] = False
        with self.assertRaises(ValidationError):
            validate_fixed_definition(definition)

    def test_same_fixed_structure_with_different_requested_tests(self):
        first = self.report(["temperature_rise", "short_circuit"])
        second = self.report(["pressure_vacuum", "oil_leakage"])
        a, b = compile_fixed(first), compile_fixed(second)
        self.assertEqual(
            [page["title"] for page in a["pages"]], [page["title"] for page in b["pages"]]
        )
        self.assertEqual(
            [[block["type"] for block in page["blocks"]] for page in a["pages"]],
            [[block["type"] for block in page["blocks"]] for page in b["pages"]],
        )
        text_a = "\n".join(
            page.extract_text() or "" for page in PdfReader(BytesIO(render_fixed_pdf(first))).pages
        )
        text_b = "\n".join(
            page.extract_text() or "" for page in PdfReader(BytesIO(render_fixed_pdf(second))).pages
        )
        for heading in (page["title"] for page in a["pages"]):
            self.assertIn(heading, text_a)
            self.assertIn(heading, text_b)
        self.assertIn("A.P.TRANSFORMERS", text_a)
        self.assertIn("Not recorded", text_a)
        self.assertIn("Not requested by customer", text_a)
        self.assertIn("Not requested by customer", text_b)
        self.assertIn("SUMMARY OF RESULTS", render_fixed_html(first))


class FixedMappingEditorTests(TestCase):
    def test_admin_change_creates_new_version_and_fresh_draft(self):
        user = get_user_model().objects.create_superuser(
            "template_admin", "admin@example.test", "local-secret"
        )
        self.client.force_login(user)
        template = ReportTemplate.objects.create(
            name="CPRI-SCL-TR-v1", version=1, definition=default_fixed_definition()
        )
        job = Job.objects.create(
            owner=user,
            title="Mapping demo",
            customer="Demo",
            sample_code="SYN-001",
            test_series="SYN-T1",
            report_template=template,
        )
        self.assertEqual(
            self.client.get(reverse("edit_fixed_mapping", args=[job.pk])).status_code, 200
        )
        response = self.client.post(
            reverse("edit_fixed_mapping", args=[job.pk]),
            {
                "placeholder": "witness_name",
                "form_type": "customer_request",
                "field_key": "customer_address",
                "unit": "",
                "decimals": "",
                "label": "Witness",
                "job_version": job.version,
            },
        )
        self.assertEqual(response.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.report_template.version, 2)
        self.assertEqual(
            job.report_template.definition["bindings"]["witness_name"]["key"], "customer_address"
        )
        report = job.reports.get()
        self.assertEqual(report.snapshot["template_mapping"]["version"], 2)
        self.assertEqual(self.client.get(reverse("report_pdf", args=[report.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("report_html", args=[report.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("report_pdf", args=[report.pk])).status_code, 200)
