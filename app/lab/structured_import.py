"""Strict, atomic CSV ingestion with explicit provenance and no auto-verification."""

import csv
import hashlib
import io

from django.db import transaction
from django.db.models import F

from .extraction import schemas
from .models import AuditEvent, Field, Job
from .report_workflow import job_locked
from .roles import station_edit_allowed


def import_csv(job, upload, reference, user):
    if upload.size > 2 * 1024 * 1024:
        raise ValueError("CSV must be smaller than 2 MB.")
    content = upload.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Save the CSV as UTF-8.") from None
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames not in (
        ["form_type", "key", "value", "unit"],
        ["form_type", "page", "key", "value", "unit"],
    ):
        raise ValueError("Columns must be form_type,page,key,value,unit in that order.")
    known = {
        s["form_type"]: {(f["page"], f["key"]): f["label"] for f in s["fields"]} for s in schemas()
    }
    rows, seen = [], set()
    for n, row in enumerate(reader, 2):
        if n > 2001:
            raise ValueError("Import at most 2,000 readings per file.")
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f"Row {n}: wrong column count.")
        kind, key = row["form_type"].strip(), row["key"].strip()
        candidates = [page for page, name in known.get(kind, {}) if name == key]
        try:
            page = (
                int(row["page"]) if "page" in row else candidates[0] if len(candidates) == 1 else 0
            )
        except ValueError:
            raise ValueError(f"Row {n}: page must be an integer.") from None
        if kind not in known or (page, key) not in known[kind]:
            raise ValueError(f"Row {n}: unknown or ambiguous page/field. Download the template.")
        if (kind, page, key) in seen:
            raise ValueError(f"Row {n}: duplicate field; split different tests into separate jobs.")
        if len(row["value"]) > 10000 or len(row["unit"]) > 30:
            raise ValueError(f"Row {n}: reading or unit is too long.")
        seen.add((kind, page, key))
        rows.append((kind, page, key, row["value"], row["unit"], known[kind][(page, key)]))
    if not rows:
        raise ValueError("The CSV contains no readings.")
    sha = hashlib.sha256(content).hexdigest()
    with transaction.atomic():
        job = Job.objects.select_for_update().get(pk=job.pk)
        if job_locked(job):
            raise ValueError("Report data is locked for review or issuance.")
        if any(not station_edit_allowed(user, job, kind) for kind, _, _, _, _, _ in rows):
            raise ValueError(
                "You are not assigned to every test type in this import, or a test is locked."
            )
        if job.fields.filter(context__import_sha256=sha).exists():
            return 0
        for kind, page, key, _, _, _ in rows:
            if (
                job.fields.filter(
                    context__form_type=kind, context__schema_key=key, document__isnull=True
                )
                .filter(context__schema_page=page)
                .exists()
            ):
                raise ValueError(
                    "These digital fields already exist. Edit their readings instead of replacing them."
                )
        Field.objects.bulk_create(
            [
                Field(
                    job=job,
                    key=f"digital.{kind}.p{page}.{key}",
                    label=label,
                    value=value,
                    raw_value=value,
                    unit=unit,
                    origin="digital",
                    status="unreviewed",
                    updated_by=user,
                    context={
                        "form_type": kind,
                        "schema_page": page,
                        "schema_key": key,
                        "source_reference": reference,
                        "import_sha256": sha,
                    },
                )
                for kind, page, key, value, unit, label in rows
            ]
        )
        Job.objects.filter(pk=job.pk).update(version=F("version") + 1)
        AuditEvent.objects.create(
            job=job,
            actor=user,
            action="structured_readings_imported",
            details={"count": len(rows), "sha256": sha, "source": reference},
        )
    return len(rows)
