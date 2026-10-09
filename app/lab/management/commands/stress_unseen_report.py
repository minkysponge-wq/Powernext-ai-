"""Import a second fictional form through the UI and time full PDF rendering."""

import hashlib
import io
import json
import math
import statistics
import time
import tracemalloc
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.test import Client
from django.urls import reverse
from pypdf import PdfReader

from lab.assembly import assemble
from lab.fixed_report import mapped_review_rows
from lab.fixed_template import TESTS
from lab.models import Field, Job, Report, ReportTemplate, Rule, TestRun
from lab.pdf_export import render_pdf
from lab.synthetic_demo import STATION_POLICIES

SAMPLE = "SYN-UNSEEN-002"
CUSTOMER = "SYNTHETIC SECOND FORM - Example Grid Labs"


class Command(BaseCommand):
    help = "Run a synthetic same-schema import, review, and fixed-PDF stress test in an empty local DB."

    def add_arguments(self, parser):
        parser.add_argument("--iterations", type=int, default=30)
        parser.add_argument(
            "--source-reference",
            default=f"{SAMPLE} fictional form export",
            help="Synthetic import label used to exercise report table wrapping.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG or Job.objects.exists():
            raise CommandError("Use DJANGO_DEBUG=1 and a fresh, isolated database with no jobs.")
        iterations = options["iterations"]
        if not 1 <= iterations <= 100:
            raise CommandError("Choose 1 to 100 render iterations.")
        root = Path(settings.BASE_DIR).parent
        csv_path = root / "demo" / "forms" / f"{SAMPLE}-readings.csv"
        if not csv_path.is_file():
            raise CommandError("Generate the second form with demo/create_unseen_form.py first.")
        if not ReportTemplate.objects.filter(name="CPRI-SCL-TR-v1", version=8).exists():
            call_command("seed_cpri_fixed_template", stdout=io.StringIO())
        if Rule.objects.filter(version=1).count() != 23:
            call_command("seed_verdict_candidates", stdout=io.StringIO())
        if Rule.objects.filter(version=2).count() != 28:
            call_command("seed_verdict_v2", stdout=io.StringIO())
        template = ReportTemplate.objects.get(name="CPRI-SCL-TR-v1", version=8)
        user = get_user_model().objects.create_user("unseen_stress_admin")
        user.is_superuser = True
        user.is_staff = True
        user.set_unusable_password()
        user.save(update_fields=["is_superuser", "is_staff", "password"])
        requested = [policy["form_type"] for policy in STATION_POLICIES]
        metadata = {
            "customer": CUSTOMER,
            "customer_address": "Fictional site, Mysuru",
            "manufacturer": "SYNTHETIC DEMO Manufacturer B",
            "sample_particulars": "Fictional 250 kVA 11 kV/433 V Dyn11 transformer - second unit",
            "requested_tests": requested,
        }
        job = Job.objects.create(
            owner=user,
            customer_user=user,
            title="Second synthetic same-schema form stress test",
            file_number="OV-SYN-UNSEEN-002",
            sample_code=SAMPLE,
            test_series="SYN-SERIES-002",
            report_template=template,
            report_scope=requested,
            report_test_ids=[row[0] for row in TESTS],
            scope_note="Fictional full-template report-generation benchmark; not CPRI evidence.",
            request_snapshot=metadata,
            **metadata,
        )
        job.rules.add(*Rule.objects.filter(version=2))
        for policy in STATION_POLICIES:
            TestRun.objects.create(
                job=job,
                test_type=policy["form_type"],
                station="Synthetic second-form bay",
                assigned_to=user,
                status="in_progress",
            )
        client = Client()
        client.force_login(user)
        started = time.perf_counter()
        content = csv_path.read_bytes()
        response = client.post(
            reverse("import_readings", args=[job.pk]),
            {
                "file": SimpleUploadedFile(csv_path.name, content, content_type="text/csv"),
                "source_reference": options["source_reference"],
            },
        )
        if response.status_code != 302:
            raise CommandError(f"CSV import failed: HTTP {response.status_code}.")
        imported = Field.objects.filter(job=job).count()
        if imported != 999 or Field.objects.filter(job=job).exclude(status="unreviewed").exists():
            raise CommandError(f"Import status mismatch: {imported} fields.")
        import_seconds = time.perf_counter() - started
        before = assemble(job)
        if not mapped_review_rows(before):
            raise CommandError("Unreviewed imported values were not marked pending.")

        started = time.perf_counter()
        fields = list(job.fields.order_by("key"))
        for page_number in range(1, math.ceil(len(fields) / 30) + 1):
            current = fields[(page_number - 1) * 30 : page_number * 30]
            payload = {
                "form-TOTAL_FORMS": len(current),
                "form-INITIAL_FORMS": len(current),
                "form-MIN_NUM_FORMS": 0,
                "form-MAX_NUM_FORMS": 1000,
                "action": "save",
            }
            for index, field in enumerate(current):
                payload.update(
                    {
                        f"form-{index}-id": field.pk,
                        f"form-{index}-value": field.value,
                        f"form-{index}-unit": field.unit,
                        f"form-{index}-status": "verified" if field.value else "not_applicable",
                        f"form-{index}-version": field.version,
                    }
                )
            response = client.post(
                reverse("digital_review", args=[job.pk]) + f"?page={page_number}", payload
            )
            if response.status_code != 302:
                raise CommandError(
                    f"Review failed on page {page_number}: HTTP {response.status_code}; "
                    f"{response.content.decode('utf-8', errors='replace')[-500:]}"
                )
        review_seconds = time.perf_counter() - started
        after = assemble(job)
        pending = mapped_review_rows(after)
        if pending:
            raise CommandError(f"{len(pending)} template readings remain pending after review.")
        if job.fields.filter(status="unreviewed").exists():
            raise CommandError("An imported field remains unreviewed.")
        response = client.post(reverse("make_report", args=[job.pk]))
        if response.status_code != 302:
            raise CommandError(f"Draft creation failed: HTTP {response.status_code}.")
        report = Report.objects.filter(job=job).order_by("-revision").first()
        if report is None or report.snapshot.get("template_mapping", {}).get("definition", {}).get("format") != "CPRI-SCL-TR-v1":
            raise CommandError("The fixed template was not used.")

        output = root / "output" / "stress-unseen"
        output.mkdir(parents=True, exist_ok=True)
        timings, sizes = [], []
        tracemalloc.start()
        for index in range(iterations):
            start = time.perf_counter()
            pdf_bytes = render_pdf(report)
            timings.append(time.perf_counter() - start)
            sizes.append(len(pdf_bytes))
            if index == 0:
                (output / f"{SAMPLE}-DRAFT.pdf").write_bytes(pdf_bytes)
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        reader = PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if page_count != 18 or "[pending review]" in pdf_text or SAMPLE not in pdf_text:
            raise CommandError("Rendered PDF failed page count, pending-value, or identity checks.")
        if report.approved_at or report.approved_pdf:
            raise CommandError("The stress test must leave this synthetic report unissued.")
        ordered = sorted(timings)
        result = {
            "sample": SAMPLE,
            "form_rows": imported,
            "imported_as_unreviewed": True,
            "ui_review_pages": math.ceil(imported / 30),
            "template_pending_after_review": len(pending),
            "page_count": page_count,
            "draft_only": True,
            "iterations": iterations,
            "import_seconds": round(import_seconds, 3),
            "review_seconds": round(review_seconds, 3),
            "render_min_seconds": round(min(timings), 3),
            "render_median_seconds": round(statistics.median(timings), 3),
            "render_p95_seconds": round(ordered[math.ceil(.95 * iterations) - 1], 3),
            "render_max_seconds": round(max(timings), 3),
            "render_peak_python_mib": round(peak_bytes / (1024 * 1024), 2),
            "pdf_bytes": sizes[0],
            "pdf_sha256": hashlib.sha256((output / f"{SAMPLE}-DRAFT.pdf").read_bytes()).hexdigest(),
            "rule_verdicts": {
                verdict: sum(1 for rule in after["calculations"] if rule["verdict"] == verdict)
                for verdict in sorted({rule["verdict"] for rule in after["calculations"]})
            },
            "blockers": after["blockers"],
        }
        (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        self.stdout.write(json.dumps(result, indent=2))
