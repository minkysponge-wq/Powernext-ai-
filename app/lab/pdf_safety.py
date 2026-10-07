"""Run untrusted PDF parsing outside the web/worker process."""

import json
import subprocess
import sys

from django.conf import settings

from .pdf_probe import MAX_PDF_BYTES


def probe_pdf(content, include_text=False):
    if not 0 < len(content) <= MAX_PDF_BYTES:
        raise ValueError("PDF exceeds 20 MB.")
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
        raise ValueError("Invalid PDF magic bytes or trailer.")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "lab.pdf_probe", *(["--text"] if include_text else [])],
            input=content,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=settings.PDF_PARSE_TIMEOUT,
            cwd=settings.BASE_DIR,
            check=True,
        )
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ValueError("PDF could not be parsed safely within the time limit.") from exc
