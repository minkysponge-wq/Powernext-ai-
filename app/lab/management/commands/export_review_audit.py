"""Export workload evidence and a blank, prediction-free transcription sheet."""

import csv
import json
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from lab.models import Job
from lab.quality import assess, identity


class Command(BaseCommand):
    help = "Export review workload and blank independent-label sheet; never mark labels verified."

    def add_arguments(self, parser):
        parser.add_argument("--job", required=True)
        parser.add_argument("--output", type=Path, required=True)

    def handle(self, *args, **options):
        job = Job.objects.get(pk=options["job"])
        output = options["output"]
        if output.exists() and any(output.iterdir()):
            raise CommandError(
                "Choose an empty output directory; existing labels will not be overwritten."
            )
        output.mkdir(parents=True, exist_ok=True)
        fields = list(job.fields.select_related("document").order_by("document_id", "page", "pk"))
        checks = assess(fields)
        counts = Counter(c["state"] for c in checks.values())
        issues = Counter(
            issue
            for c in checks.values()
            for issue in set(c["issues"])
            if not issue.startswith(("Mean check:", "Sum check:", "Difference check:"))
        )
        documents = [
            {
                "id": str(d.pk),
                "name": d.original_name,
                "sha256": d.sha256,
                "pages": d.page_count,
                "source_url": reverse("source", args=[d.pk]),
            }
            for d in job.documents.all()
        ]
        with (output / "labels-to-check.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                [
                    "field_id",
                    "form_type",
                    "page",
                    "key",
                    "source",
                    "expected_value",
                    "expected_unit",
                    "independently_verified",
                    "unseen",
                ]
            )
            for f in fields:
                kind, page, key = identity(f)
                writer.writerow(
                    [
                        f.pk,
                        kind,
                        page,
                        key,
                        (
                            f.document.original_name
                            if f.document
                            else f.context.get("source_reference", "Digital entry")
                        ),
                        "",
                        "",
                        "",
                        "",
                    ]
                )
        audit = {
            "job_id": str(job.pk),
            "fields": len(fields),
            "states": dict(counts),
            "issue_counts": dict(issues),
            "arithmetic_discrepancy_fields": sum(bool(c["failed"]) for c in checks.values()),
            "blank_fields": sum(not f.value.strip() for f in fields),
            "missing_unit_fields": sum(not f.unit.strip() for f in fields),
            "documents": documents,
            "false_accept_rate": None,
            "limitation": "Workload audit only. Automatic checking is not correctness. Labels remain blank until independently transcribed.",
        }
        (output / "workload.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
        (output / "README.txt").write_text(
            "Sampeeth: transcribe from the original source PDFs, without consulting model predictions.\n"
            "Fill expected_value and expected_unit; leave genuinely blank values blank. Mark independently_verified=yes only after your check.\n"
            "Mark unseen=yes only for forms held out from development. The supplied reference forms have been used in development and are NOT unseen.\n"
            "This export deliberately omits predicted answers. Do not use this development pack to claim unseen accuracy.\n"
            "Keep all rows. Do not change field IDs. Record reviewer, date and ambiguities in a separate review note.\n",
            encoding="utf-8",
        )
        self.stdout.write(
            json.dumps(
                {k: v for k, v in audit.items() if k not in ("documents", "issue_counts")}, indent=2
            )
        )
