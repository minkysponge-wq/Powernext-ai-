"""Isolated PDF metadata/text probe, launched with a strict wall-clock timeout."""

import json
import sys
from io import BytesIO

from pypdf import PdfReader

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 30


def inspect_pdf(content, include_text=False):
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
        raise ValueError("Invalid PDF signature or trailer.")
    if not 0 < len(content) <= MAX_PDF_BYTES:
        raise ValueError("PDF size is outside the supported limit.")
    reader = PdfReader(BytesIO(content), strict=True)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
        raise ValueError("Encrypted PDF or unsupported page count.")
    result = {"pages": len(reader.pages)}
    if include_text:
        result["text"] = "\n".join((page.extract_text() or "")[:10000] for page in reader.pages)[
            :200000
        ]
    return result


if __name__ == "__main__":
    try:
        print(
            json.dumps(inspect_pdf(sys.stdin.buffer.read(MAX_PDF_BYTES + 1), "--text" in sys.argv))
        )
    except Exception:
        sys.exit(2)
