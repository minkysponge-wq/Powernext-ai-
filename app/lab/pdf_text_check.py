"""Fail issuance if customer-request text is absent from the rendered PDF."""

import re
import unicodedata
from io import BytesIO

from pypdf import PdfReader

CUSTOMER_FIELDS = (
    ("customer", "Customer"),
    ("customer_address", "Customer address"),
    ("manufacturer", "Manufacturer"),
    ("sample_particulars", "Sample particulars"),
)


def _space(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(text))).strip()


def verify_request_pdf_text(pdf_bytes, request_snapshot):
    """Check visible label/value text exactly apart from PDF line wrapping.

    This is a rendering check, not a substitute for human review of the
    submitted request or the official certificate layout.
    """
    if not request_snapshot:
        return []  # Legacy jobs have no digital customer request to compare.
    try:
        pdf = PdfReader(BytesIO(pdf_bytes), strict=True)
        text = _space(" ".join(page.extract_text() or "" for page in pdf.pages))
    except Exception as error:
        raise ValueError("Issued PDF text could not be inspected.") from error
    mismatched = []
    for key, label in CUSTOMER_FIELDS:
        if key in request_snapshot:
            expected = _space(request_snapshot[key])
            if not expected or _space(f"{label}: {expected}") not in text:
                mismatched.append(key)
    return mismatched
