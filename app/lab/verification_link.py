"""Build a public verification link from an explicitly configured base URL."""

from django.conf import settings


def report_verification_url(report, code):
    base = settings.PUBLIC_VERIFY_BASE_URL.rstrip("/")
    if not base or "example.org" in base or not base.startswith(("http://", "https://")):
        raise ValueError("A real VECTORLAB_PUBLIC_BASE_URL is required before issue.")
    return f"{base}/verify/{report.pk}/{code}/"
