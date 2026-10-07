"""Audit-import only human-confirmed values from the 37-cell review handoff.

The workbook itself has blank decision/reviewer columns. Running --apply therefore
requires a separate explicit user confirmation, recorded in the command output
and the audit event. The two '(your decision)' rows are always skipped.
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DJANGO_DEBUG", "1")
import django

django.setup()

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F
from lab.models import AuditEvent, Field, Job
from lab.report_builder import save_draft
from lab.report_workflow import job_locked

WORKBOOK = Path(
    os.environ.get(
        "VECTORLAB_PRIVATE_REVIEW_WORKBOOK", ROOT / "private" / "reference" / "review_37.xlsx"
    )
)
JOB_ID = os.environ.get("VECTORLAB_PRIVATE_REVIEW_JOB_ID", "")
MAPPING = (
    [
        ("routine_test", key)
        for key in (
            "hv_voltage_BT",
            "hv_duration_BT",
            "lv_voltage_BT",
            "lv_duration_BT",
            "hv_voltage_AT",
            "hv_duration_AT",
            "lv_voltage_AT",
            "lv_duration_AT",
            "date_BT",
            "date_AT",
            "instrument_serials",
            "induced_voltage_BT",
            "induced_frequency_BT",
            "induced_duration_BT",
            "induced_voltage_AT",
            "induced_frequency_AT",
            "induced_duration_AT",
        )
    ]
    + [
        ("loss_measurement", "date_BT"),
        ("loss_measurement", "date_AT"),
        ("temperature_rise", "test_dates"),
        ("temperature_rise", "instrument_serials"),
        ("short_circuit", "conductor_core_clamps"),
        ("short_circuit", "spacers"),
        ("short_circuit", "oil"),
        ("short_circuit", "date"),
    ]
    + [
        ("pressure_oil_leakage", key)
        for key in (
            "type_pressure",
            "type_pressure_duration",
            "pressure_deflection_result",
            "vacuum",
            "vacuum_duration",
            "vacuum_deflection_result",
            "pressure_date",
            "type_date",
            "instrument_serials",
            "oil_date",
        )
    ]
    + [("customer_request", "witness_name"), ("routine_test", "lv_observation_BT")]
)
FLAGGED = {10, 20, 32, 33}  # zero-based: three blank-serial rows and combined type dates
UNANSWERED = {35, 36}
NEW_KEYS = {"pressure_deflection_result", "vacuum_deflection_result"}


def rows():
    sheet = load_workbook(WORKBOOK, read_only=True, data_only=True)["Pending fields"]
    values = [tuple(row) for row in sheet.values]
    listed = values[1:38]
    if len(listed) != 37 or len(MAPPING) != 37 or any(not row[1] for row in listed):
        raise ValueError("Expected 37 labeled review rows.")
    if [listed[i][2] for i in (35, 36)] != ["(your decision)", "(your decision)"]:
        raise ValueError("Unanswered source rows changed; recheck the workbook.")
    return listed


def selected_indexes(reviewed, include_flagged=False):
    return [
        i
        for i, row in enumerate(reviewed)
        if i not in UNANSWERED
        and (i not in FLAGGED or include_flagged)
        and str(row[2] or "").strip()
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirmed-by-user",
        action="store_true",
        help="Human explicitly confirmed non-flagged source values in this chat.",
    )
    parser.add_argument(
        "--include-flagged",
        action="store_true",
        help="Human explicitly confirmed the flagged source rows too.",
    )
    args = parser.parse_args()
    reviewed = rows()
    job = Job.objects.get(pk=JOB_ID)
    if job_locked(job) or job.reports.filter(approved_at__isnull=False).exists():
        raise ValueError("Sample job is locked or issued.")
    if args.apply and not args.confirmed_by_user:
        raise ValueError(
            "The workbook has no signed reviewer decisions; explicit human confirmation is required."
        )
    selected = selected_indexes(reviewed, args.include_flagged)
    checks = []
    for i in selected:
        form, key = MAPPING[i]
        matches = list(
            Field.objects.filter(
                job=job, document__form_type=form, context__schema_key=key
            ).select_related("document")
        )
        if key in NEW_KEYS:
            if matches:
                raise ValueError(f"New source field already exists: {form}.{key}")
            doc = job.documents.filter(form_type=form).first()
            if doc is None:
                raise ValueError(f"Missing source PDF: {form}")
            field = None
        else:
            if len(matches) != 1:
                raise ValueError(f"Expected one source field: {form}.{key}; found {len(matches)}")
            field = matches[0]
            doc = field.document
            if (
                field.status not in ("unreviewed", "ambiguous")
                or field.page != 1
                and not (
                    form == "temperature_rise" and key == "instrument_serials" and field.page == 2
                )
            ):
                raise ValueError(f"Source field changed or is already reviewed: {form}.{key}")
        if doc.job_id != job.pk:
            raise ValueError(f"Source PDF belongs to another job: {form}.{key}")
        checks.append((i, form, key, field, doc))
    digest = hashlib.sha256(WORKBOOK.read_bytes()).hexdigest()
    summary = {
        "workbook_sha256": digest,
        "selected": len(checks),
        "flagged_included": args.include_flagged,
        "unanswered": [(MAPPING[i][0], MAPPING[i][1]) for i in sorted(UNANSWERED)],
        "reviewer": "sampreeth" if args.confirmed_by_user else None,
    }
    if not args.apply:
        print(json.dumps({"mode": "preflight", **summary}, indent=2))
        return
    actor = get_user_model().objects.get(username="sampeeth")
    with transaction.atomic():
        job = Job.objects.select_for_update().get(pk=JOB_ID)
        if job_locked(job):
            raise ValueError("Job became locked.")
        for i, form, key, prior, doc in checks:
            row = reviewed[i]
            value = str(row[2]).strip()
            unit = str(row[3] or "").strip()
            if prior is None:
                source_key = f"{doc.sha256[:24]}.{form}.p1.{key}"
                if Field.objects.filter(job=job, key=source_key).exists():
                    raise ValueError(f"New key already exists: {source_key}")
                field = Field.objects.create(
                    job=job,
                    key=source_key,
                    label=str(row[1]),
                    value=value,
                    unit=unit,
                    raw_value="",
                    origin="scan",
                    document=doc,
                    page=1,
                    status="verified",
                    context={
                        "schema_key": key,
                        "schema_page": 1,
                        "form_type": form,
                        "extraction_status": "not_extracted",
                        "source_review_workbook_sha256": digest,
                    },
                    updated_by=actor,
                )
                before = None
            else:
                field = Field.objects.select_for_update().get(pk=prior.pk)
                if (field.version, field.value, field.unit, field.status) != (
                    prior.version,
                    prior.value,
                    prior.unit,
                    prior.status,
                ):
                    raise ValueError(f"Field changed during import: {form}.{key}")
                before = {"value": field.value, "unit": field.unit, "status": field.status}
                field.value = value
                field.unit = unit
                field.status = "verified"
                field.updated_by = actor
                field.version += 1
                field.save(
                    update_fields=["value", "unit", "status", "updated_by", "version", "updated_at"]
                )
            AuditEvent.objects.create(
                job=job,
                actor=actor,
                action="reading_created" if before is None else "reading_updated",
                details={
                    "key": field.key,
                    "before": before,
                    "after": {"value": field.value, "unit": field.unit, "status": field.status},
                    "source_review": {
                        "reviewer_name": "sampreeth",
                        "workbook_sha256": digest,
                        "row": i + 2,
                        "source": row[4],
                        "confirmation": "human verified source scans in chat",
                    },
                },
            )
        Job.objects.filter(pk=job.pk).update(version=F("version") + len(checks))
        AuditEvent.objects.create(
            job=job, actor=actor, action="pending_37_source_review_imported", details=summary
        )
        report, created = save_draft(job, actor, trigger="pending_37_source_review_import")
        summary.update(report_revision=report.revision, draft_created=created)
    print(json.dumps({"mode": "applied", **summary}, indent=2))


if __name__ == "__main__":
    main()
