"""Build a public verification link from an explicitly configured base URL."""

from django.conf import settings


def issued_report_number(report):
    """Allocate a distinct visible number only when a synthetic report is issued."""
    if report.approved_at and report.snapshot.get("synthetic_demo"):
        return f"VL-SCL-{report.approved_at.year}-{report.pk.hex[:10].upper()}"
    return report.snapshot.get("file_number") or "Not allocated"


def report_verification_url(report, code):
    base = settings.PUBLIC_VERIFY_BASE_URL.rstrip("/")
    if not base or "example.org" in base or not base.startswith(("http://", "https://")):
        raise ValueError("A real VECTORLAB_PUBLIC_BASE_URL is required before issue.")
    return f"{base}/verify/{report.pk}/{code}/"
