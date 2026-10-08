"""The public walkthrough must start before any approval or issue."""

from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice
from pypdf import PdfReader

from .assembly import assemble
from .calculations import evaluate_rule
from .fixed_report import mapped_review_rows
from .management.commands.seed_verdict_candidates import candidates
from .management.commands.seed_verdict_v2 import v2_definitions
from .models import Job, Report
from .pdf_export import render_pdf
from .pdf_text_check import verify_request_pdf_text
from .report_workflow import issue_code, verify_issue_seal
from .test_pdf_signing import demo_identity


class SyntheticSeedTests(TestCase):
    def test_synthetic_job_issues_only_after_three_mfa_roles(self):
        with (
            TemporaryDirectory() as directory,
            override_settings(
                DEBUG=True,
                BASE_DIR=Path(directory) / "app",
                MEDIA_ROOT=Path(directory) / "media",
                MFA_ENFORCED=False,
            ),
        ):
            call_command("seed_cpri_fixed_template", stdout=StringIO())
            call_command("seed_walkthrough_demo", stdout=StringIO())
            job = Job.objects.get(sample_code="SYN-FULL-001")
            p12, _ = demo_identity(directory)
            with override_settings(
                MFA_ENFORCED=True,
                PDF_SIGNING_P12_PATH=str(p12),
                PDF_SIGNING_P12_PASSWORD="fixture-password",
                PUBLIC_VERIFY_BASE_URL="http://127.0.0.1:8000",
            ):
                clients = {}
                for role in ("admin", "engineer_a", "engineer_b", "quality", "hod"):
                    user = get_user_model().objects.get(username="walkthrough_" + role)
                    client = Client()
                    client.force_login(user)
                    device = TOTPDevice.objects.get(user=user, confirmed=True)
                    response = client.post(
                        reverse("otp_verify"), {"token": str(totp(device.bin_key)).zfill(6)}
                    )
                    self.assertEqual(response.status_code, 302, (role, response.content[:200]))
                    clients[role] = client
                for run in job.test_runs.select_related("assigned_to"):
                    role = run.assigned_to.username.removeprefix("walkthrough_")
                    response = clients[role].post(
                        reverse("lock_test", args=[run.pk]), {"confirmed": "yes"}
                    )
                    self.assertEqual(
                        response.status_code, 302, (run.test_type, response.content[:200])
                    )
                response = clients["admin"].post(reverse("make_report", args=[job.pk]))
                self.assertEqual(response.status_code, 302, response.content[:200])
                report = Report.objects.filter(job=job).order_by("-revision").first()
                self.assertIsNotNone(report)
                self.assertEqual(
                    clients["engineer_a"]
                    .post(reverse("engineer_lock", args=[report.pk]), {"confirmed": "yes"})
                    .status_code,
                    302,
                )
                self.assertEqual(
                    clients["quality"]
                    .post(reverse("quality_verify", args=[report.pk]), {"confirmed": "yes"})
                    .status_code,
                    302,
                )
                issue = clients["hod"].post(
                    reverse("approve_report", args=[report.pk]), {"confirmed": "yes"}
                )
                self.assertEqual(issue.status_code, 302, issue.content[:300])
                report.refresh_from_db()
                self.assertTrue(verify_issue_seal(report))
                with report.approved_pdf.open("rb") as saved:
                    pdf = saved.read()
                self.assertIn(b"/ByteRange", pdf)
                issued_pages = PdfReader(BytesIO(pdf)).pages
                self.assertEqual(len(issued_pages), 18)
                issued_text = "\n".join(page.extract_text() or "" for page in issued_pages)
                self.assertNotIn("final approval is required before issue", issued_text)
                self.assertIn("walkthrough_engineer_a", issued_text)
                self.assertIn("walkthrough_quality", issued_text)
                self.assertIn("walkthrough_hod", issued_text)
                self.assertIn("Tested by (Engineer)", issued_text)
                self.assertRegex(issued_text, r"walkthrough_hod\s+—\s+\d{2} \w+ \d{4}, \d{2}:\d{2}")
                self.assertContains(
                    clients["hod"].get(reverse("report", args=[report.pk])),
                    reverse("verify_issued", args=[report.pk, issue_code(report)]),
                )
                self.assertContains(
                    clients["hod"].get(
                        reverse("verify_issued", args=[report.pk, issue_code(report)])
                    ),
                    "VALID",
                )

    def test_one_full_template_job_is_seeded_without_approval(self):
        with (
            TemporaryDirectory() as directory,
            override_settings(DEBUG=True, BASE_DIR=Path(directory) / "app", MFA_ENFORCED=True),
        ):
            call_command("seed_cpri_fixed_template", stdout=StringIO())
            call_command("seed_walkthrough_demo", stdout=StringIO())
            job = Job.objects.get(sample_code="SYN-FULL-001")
            self.assertEqual(job.report_template.name, "CPRI-SCL-TR-v1")
            self.assertEqual(job.report_template.version, 8)
            self.assertEqual(job.test_runs.count(), 7)
            self.assertEqual(job.test_runs.filter(status="in_progress").count(), 7)
            self.assertEqual(job.test_runs.filter(assigned_to__isnull=False).count(), 7)
            self.assertEqual(len(job.report_test_ids), 12)
            self.assertFalse(Report.objects.filter(job=job).exists())
            for name in ("walkthrough_engineer_a", "walkthrough_quality", "walkthrough_hod"):
                user = get_user_model().objects.get(username=name)
                self.assertTrue(TOTPDevice.objects.filter(user=user, confirmed=True).exists())
            data = assemble(job)
            self.assertEqual(data["blockers"], 0, data["findings"])
            self.assertEqual(job.rules.count(), 23)
            self.assertEqual(
                [
                    (rule["code"], rule["verdict"])
                    for rule in data["calculations"]
                    if rule["verdict"] != "pass"
                ],
                [],
            )
            self.assertEqual(mapped_review_rows(data), [])
            preview = SimpleNamespace(
                snapshot=data, approved_at=None, revision=1, pk="synthetic-preview"
            )
            preview_pdf = render_pdf(preview)
            self.assertEqual(verify_request_pdf_text(preview_pdf, job.request_snapshot), [])
            pages = PdfReader(BytesIO(preview_pdf)).pages
            self.assertEqual(len(pages), 18)
            cover = pages[0].extract_text() or ""
            self.assertIn("Routine test", cover)
            self.assertIn("Short-circuit test", cover)
            self.assertNotIn("routine_test", cover)
            self.assertNotIn("short_circuit", cover)
            self.assertNotIn("['", cover)
            text = "\n".join(page.extract_text() or "" for page in pages)
            self.assertNotIn("[pending review]", text)
            prior = []
            for definition in v2_definitions(
                [
                    SimpleNamespace(code=code, title=title, operation=operation, parameters=params)
                    for code, title, operation, params in candidates()
                ]
            ):
                rule = SimpleNamespace(**definition, version=2)
                result = evaluate_rule(
                    rule,
                    data["fields"],
                    data["transformer_calculations"],
                    {
                        "requested_tests_text": job.requested_tests,
                        "requested_test_ids": job.report_test_ids,
                        "prior_results": prior,
                    },
                )
                if rule.code != "SC_OVERALL":
                    prior.append(result)
                if rule.code == "NO_LOAD_CURRENT_112" and result["verdict"] == "not_applicable":
                    continue  # The synthetic CRF does not request this special test.
                if result["v2_status"] in ("ACTIVE", "OBSERVATION"):
                    self.assertEqual(result["verdict"], "pass", (rule.code, result["message"]))
