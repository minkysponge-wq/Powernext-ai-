"""Tamper-evident per-job audit chain; keep the signing key outside database backups."""

import hashlib
import hmac
import json

from django.conf import settings


def event_digest(event):
    payload = json.dumps(
        {
            "job": str(event.job_id),
            "actor": event.actor_id,
            "action": event.action,
            "details": event.details,
            "created_at": event.created_at.isoformat(),
            "previous_hash": event.previous_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hmac.new(settings.AUDIT_CHAIN_KEY.encode(), payload, hashlib.sha256).hexdigest()


def verify_job(job):
    from .models import AuditEvent, AuditHead

    previous = "0" * 64
    count = 0
    for event in AuditEvent.objects.filter(job=job).order_by("pk"):
        if event.previous_hash != previous or not hmac.compare_digest(
            event.event_hash, event_digest(event)
        ):
            return event.pk
        previous = event.event_hash
        count += 1
    head = AuditHead.objects.filter(job=job).first()
    if count and (head is None or head.event_count != count or head.last_hash != previous):
        return "head mismatch or missing tail"
    if not count and head and (head.event_count != 0 or head.last_hash != "0" * 64):
        return "head mismatch"
    return None
