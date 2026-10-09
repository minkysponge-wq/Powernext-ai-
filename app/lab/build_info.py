"""Expose display-only release metadata to templates."""

import subprocess
from pathlib import Path


def _git_short_hash():
    root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        return result.stdout.strip() or "dev"
    except (OSError, subprocess.SubprocessError):
        return "dev"


BUILD_HASH = _git_short_hash()


def build_info(request):
    return {"vectorlab_build_hash": BUILD_HASH}
