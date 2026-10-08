import csv
import io

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from pypdf import PdfReader

from .assembly import assemble
from .models import Document, Field, Job, ReportTemplate
from .quality import assess
from .template_mapping import default_definition
from .workflow_views import grouped_readings


class DayTwoTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("reviewer")
        self.client.force_login(self.user)
        self.job = Job.objects.create(
            owner=self.user,
            title="Synthetic review fixture",
            customer="Fixture",
            sample_code="S1",
            test_series="T1",
        )

    def field(self, key, value, unit="A", source="Sheet A", kind="loss_measurement"):
        return Field.objects.create(
            job=self.job,
            key=source + "." + key,
            label=key,
            value=value,
            unit=unit,
            raw_value=value,
            origin="digital",
            updated_by=self.user,
            context={
                "schema_key": key,
                "schema_page": 1,
                "form_type": kind,
                "source_reference": source,
            },
        )

    def table(self):
        return [
            self.field("no_load.0." + k, v)
            for k, v in [("I1", "1.00"), ("I2", "2.00"), ("I3", "3.00"), ("Iavg", "2.00")]
        ]

    def test_review_by_exception_and_one_action_for_checked_table(self):
        fields = self.table()
        critical = self.field("sample_code", "S1", "")
        checks = assess(list(self.job.fields.select_related("document")))
        self.assertTrue(all(checks[f.pk]["state"] == "automatically_checked" for f in fields))
        self.assertEqual(checks[critical.pk]["state"], "needs_review")
        response = self.client.get(reverse("exception_review", args=[self.job.pk]))
        self.assertContains(response, "Approve 4 checked readings")
        self.assertContains(response, "Exceptions only")
        groups, _ = grouped_readings(self.job)
        token = next(g["token"] for g in groups if g["token"])
        url = reverse("approve_table", args=[self.job.pk])
        self.assertEqual(self.client.post(url, {"token": token}).status_code, 400)
        self.assertEqual(
            self.client.post(url, {"token": token, "confirmed": "yes"}).status_code, 302
        )
        self.assertEqual(self.job.fields.filter(status="verified").count(), 4)
        critical.refresh_from_db()
        self.assertEqual(critical.status, "unreviewed")
        self.assertEqual(
            self.client.post(url, {"token": token, "confirmed": "yes"}).status_code, 409
        )

    def test_exception_link_opens_matching_batch_review_page(self):
        document = Document.objects.create(
            job=self.job,
            original_name="fixture.pdf",
            file="sources/fixture.pdf",
            sha256="a" * 64,
            page_count=1,
            form_type="work_instruction",
            status="ready",
        )
        for index in range(20):
            Field.objects.create(
                job=self.job,
                document=document,
                key=f"fixture.{index}",
                label=f"Ordinary {index}",
                value="Recorded",
                raw_value="Recorded",
                origin="scan",
                page=1,
                updated_by=self.user,
                context={"form_type": "work_instruction", "schema_key": f"ordinary_{index}"},
            )
        critical = Field.objects.create(
            job=self.job,
            document=document,
            key="fixture.sample",
            label="Sample identifier",
            value="S1",
            raw_value="S1",
            origin="scan",
            page=1,
            updated_by=self.user,
            context={"form_type": "work_instruction", "schema_key": "sample_code"},
        )
        response = self.client.get(
            reverse("exception_review", args=[self.job.pk]) + "?focus=priority"
        )
        self.assertContains(response, f"?page=2#reading-{critical.pk}")
        review = self.client.get(reverse("review_document", args=[document.pk]) + "?page=2")
        self.assertContains(review, f'id="reading-{critical.pk}"')

    def test_stale_and_cross_job_bulk_review_are_rejected(self):
        fields = self.table()
        groups, _ = grouped_readings(self.job)
        token = next(g["token"] for g in groups if g["token"])
        fields[0].version += 1
        fields[0].save()
        self.assertEqual(
            self.client.post(
                reverse("approve_table", args=[self.job.pk]), {"token": token, "confirmed": "yes"}
            ).status_code,
            409,
        )
        other = get_user_model().objects.create_user("outsider")
        self.client.force_login(other)
        self.assertEqual(
            self.client.get(reverse("exception_review", args=[self.job.pk])).status_code, 404
        )
        self.assertEqual(
            self.client.post(
                reverse("approve_table", args=[self.job.pk]), {"token": token, "confirmed": "yes"}
            ).status_code,
            404,
        )

    def test_misread_mean_and_near_limit_remain_exceptions(self):
        fields = self.table()
        fields[1].value = "5.00"
        fields[1].save()
        near = self.field("hv_winding_rise", "39.7", "K", kind="temperature_rise")
        checks = assess(list(self.job.fields.select_related("document")))
        self.assertTrue(all(checks[f.pk]["state"] == "needs_review" for f in fields + [near]))
        self.assertTrue(
            any(f["id"].startswith("arithmetic-") for f in assemble(self.job)["findings"])
        )

    def test_decimal_difference_is_checked_without_changing_source(self):
        fields = [
            self.field("deflection.0." + key, value, "mm", kind="pressure_oil_leakage")
            for key, value in [("initial", "259.90"), ("final", "259.64"), ("difference", "0.35")]
        ]
        checks = assess(fields)
        self.assertTrue(all(c["failed"] for c in checks.values()))
        self.assertEqual(fields[-1].value, "0.35")

    def test_configurable_mapping_freezes_and_evidence_keeps_all_readings(self):
        a = self.field("sample_code", "S1", "", kind="work_instruction")
        self.field("test_series", "T1", "", kind="work_instruction")
        definition = {
            "report_title": "Configured laboratory report",
            "sections": [
                {
                    "form_type": "work_instruction",
                    "title": "Customer-selected heading",
                    "fields": [{"key": "sample_code", "label": "Specimen identifier"}],
                }
            ],
        }
        template = ReportTemplate.objects.create(
            name="Fixture template", version=1, definition=definition
        )
        self.job.report_template = template
        self.job.save()
        self.assertContains(
            self.client.get(reverse("report_mapping", args=[self.job.pk])), "Specimen identifier"
        )
        self.client.post(reverse("make_report", args=[self.job.pk]))
        report = self.job.reports.first()
        self.assertEqual(len(report.snapshot["fields"]), 2)
        self.assertEqual(len(report.snapshot["report_sections"][0]["fields"]), 1)
        self.assertEqual(
            report.snapshot["report_sections"][0]["fields"][0]["label"], "Specimen identifier"
        )
        self.assertEqual(report.snapshot["report_title"], "Configured laboratory report")
        self.assertContains(
            self.client.get(reverse("report", args=[report.pk])), "Configured laboratory report"
        )
        pdf = self.client.get(reverse("report_pdf", args=[report.pk]))
        pdf_bytes = b"".join(pdf.streaming_content) if pdf.streaming else pdf.content
        self.assertIn(
            "Configured laboratory report",
            "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf_bytes)).pages),
        )
        template.definition["sections"][0]["title"] = "Changed"
        with self.assertRaises(ValidationError):
            template.full_clean()
        template.definition = definition | {"report_title": ""}
        with self.assertRaises(ValidationError):
            template.full_clean()
        report.refresh_from_db()
        self.assertEqual(
            report.snapshot["report_sections"][0]["title"], "Customer-selected heading"
        )
        response = self.client.get(reverse("report_evidence", args=[report.pk]))
        self.assertEqual(len(list(csv.reader(io.StringIO(response.content.decode())))), 3)

    def test_unknown_template_mapping_and_omitted_scope_rejected(self):
        template = ReportTemplate(
            name="Bad",
            definition={
                "sections": [
                    {
                        "form_type": "work_instruction",
                        "title": "Test",
                        "fields": [{"key": "invented"}],
                    }
                ]
            },
        )
        with self.assertRaises(ValidationError):
            template.full_clean()
        template.definition = default_definition()
        template.full_clean()
        template.definition = {"sections": [template.definition["sections"][0]]}
        template.save()
        self.job.report_template = template
        self.job.report_scope = ["pressure_oil_leakage"]
        self.job.save()
        self.assertTrue(
            any(f["id"] == "unmapped-pressure_oil_leakage" for f in assemble(self.job)["findings"])
        )

    def test_missing_units_do_not_qualify_even_when_sum_matches(self):
        fields = [
            self.field("no_load.0." + k, v, "")
            for k, v in [("W1", "10.0"), ("W2", "20.0"), ("W3", "30.0"), ("Wtotal", "60.0")]
        ]
        checks = assess(fields)
        self.assertTrue(all(c["passed"] and c["state"] == "needs_review" for c in checks.values()))
        fields[-1].value = "80.0"
        self.assertTrue(all(c["failed"] for c in assess(fields).values()))

    def test_legacy_keys_receive_arithmetic_checks(self):
        fields = self.table()
        for f in fields:
            f.key = "loss_measurement.p1." + f.context.pop("schema_key")
        fields[-1].value = "9.00"
        self.assertTrue(all(c["failed"] for c in assess(fields).values()))
