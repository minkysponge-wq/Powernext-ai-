"""Replay a synthetic, multi-role station workflow through the app's HTTP screens.

The original source-backed HVD replay is kept outside Git in private/demo/.
No customer readings or source PDFs are needed by this public demo.
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DJANGO_DEBUG", "1")
os.environ.setdefault("PAID_AI_ALLOWED", "0")

import django

django.setup()

from django.conf import settings
from django.core.management import call_command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Create the synthetic walkthrough jobs."
    )
    args = parser.parse_args()
    if not settings.DEBUG:
        raise SystemExit("Synthetic replay is available only in local DEBUG mode.")
    if args.apply:
        call_command("seed_walkthrough_demo")
    else:
        print(
            json.dumps(
                {
                    "mode": "preflight",
                    "dataset": "synthetic only",
                    "stages": [
                        "customer request",
                        "station entry",
                        "Engineer lock",
                        "Quality",
                        "HoD issue",
                    ],
                    "apply": "python demo/seed_station_entry.py --apply",
                    "access_file": "output/walkthrough-demo-access.json (created on apply; private)",
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
