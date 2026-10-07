"""Idempotent draft snapshot assembly shared by manual and station workflows."""

import hashlib
import json

from .assembly import assemble
from .models import AuditEvent, Report


def save_draft(job, actor, trigger="manual"):
    snapshot = assemble(job)
    approved = job.insight_drafts.filter(
        evidence_hash=snapshot["evidence_hash"], status="approved"
    ).first()
    snapshot["ai_insights"] = approved.content if approved else []
    snapshot["ai_review"] = (
        {
            "provider": approved.provider,
            "reviewer": approved.reviewed_by.username,
            "reviewed_at": approved.reviewed_at.isoformat(),
        }
        if approved
        else None
    )
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    existing = job.reports.filter(snapshot_sha256=digest).first()
    if existing:
        return existing, False
    latest = job.reports.order_by("-revision").first()
    search_text = " ".join(
        str(value)
        for value in [
            snapshot.get("customer", ""),
            snapshot.get("title", ""),
            snapshot.get("sample_code", ""),
            snapshot.get("test_series", ""),
            snapshot.get("file_number", ""),
            *[
                field.get("label", "") + " " + field.get("value", "")
                for field in snapshot.get("fields", [])
            ],
            *[
                rule.get("title", "") + " " + rule.get("source_clause", "")
                for rule in snapshot.get("calculations", [])
            ],
        ]
        if value
    )
    report = Report.objects.create(
        job=job,
        revision=latest.revision + 1 if latest else 1,
        snapshot=snapshot,
        snapshot_sha256=digest,
        search_text=search_text,
        created_by=actor,
        template_version="mapped-v" + str(snapshot["template_mapping"]["version"]),
    )
    AuditEvent.objects.create(
        job=job,
        actor=actor,
        action="draft_snapshot_created",
        details={"report": str(report.pk), "trigger": trigger},
    )
    return report, True
