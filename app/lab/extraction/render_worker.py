"""Process entry point for isolated PDF page rendering."""

import sys
from pathlib import Path

from .rendering import _render_page

if __name__ == "__main__":
    try:
        path = Path(sys.argv[1]).resolve()
        page = int(sys.argv[2])
        side = int(sys.argv[3])
        if not 1 <= page <= 30 or not 300 <= side <= 2400:
            raise ValueError("Unsupported rendering dimensions.")
        sys.stdout.buffer.write(_render_page(path, page, side))
    except Exception:
        sys.exit(2)
