import subprocess
import sys
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
from django.conf import settings


def _render_page(pdf_path, page_number, max_side=2400):
    with pdfium.PdfDocument(str(pdf_path)) as pdf:
        page = pdf[page_number - 1]
        try:
            scale = max_side / max(page.get_size())
            bitmap = page.render(scale=scale)
            try:
                image = bitmap.to_pil().convert("RGB")
                stream = BytesIO()
                image.save(stream, format="PNG")
                return stream.getvalue()
            finally:
                bitmap.close()
        finally:
            page.close()


def render_page(pdf_path, page_number, max_side=2400):
    """Bound untrusted PDF rendering, including review previews, by a wall-clock limit."""
    if not 1 <= int(page_number) <= 30 or not 300 <= int(max_side) <= 2400:
        raise ValueError("PDF page or rendering size is outside the supported range.")
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "lab.extraction.render_worker",
                str(pdf_path),
                str(page_number),
                str(max_side),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=settings.PDF_RENDER_TIMEOUT,
            cwd=settings.BASE_DIR,
            check=True,
        )
        if not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("PDF preview did not render as PNG.")
        return result.stdout
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        raise ValueError("PDF preview could not be rendered within the time limit.") from exc


@lru_cache(maxsize=8)
def _cached_review_page(path, page_number, max_side, modified_ns, size):
    """Keep only eight rendered pages in process memory for adjacent field crops."""
    return render_page(path, page_number, max_side)


def render_review_page(pdf_path, page_number, max_side=2400):
    """Reuse a source page after authorization; invalidate when its file changes."""
    path = Path(pdf_path).resolve(strict=True)
    stat = path.stat()
    return _cached_review_page(
        str(path), int(page_number), int(max_side), stat.st_mtime_ns, stat.st_size
    )
