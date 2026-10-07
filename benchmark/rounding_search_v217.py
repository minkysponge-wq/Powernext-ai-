"""Search common worksheet intermediate-rounding policies against a synthetic v2.17 fixture.

This is diagnostic. It never alters the calculation engine to force a match.
"""

import itertools
import json
from math import sqrt
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from lab.transformer import calculate_datasheet

VALUES = json.loads(
    (ROOT / "app/lab/fixtures/synthetic_datasheet_v217.json").read_text(encoding="utf-8")
)["values"]
PRINTED = {
    ("NTBT", "total_loss_50"): 917.94,
    ("NTAT", "total_loss_50"): 915.55,
    ("NTBT", "total_loss_100"): 2473.97,
    ("NTAT", "total_loss_100"): 2470.48,
    ("HTBT", "total_loss_100"): 2423.27,
    ("HTAT", "total_loss_100"): 2419.94,
    ("LTBT", "total_loss_100"): 2642.74,
    ("LTAT", "total_loss_100"): 2639.45,
}


def _rounded(value, places):
    return value if places is None else round(value, places)


def candidate(policy, values=VALUES):
    sqrt3, mean_places, test_places, copper_places, factor_places, output_places = policy
    n = lambda key: float(values[key])
    avg = lambda prefix, col: _rounded(
        sum(n(f"{prefix}.{col}{j}") for j in (1, 2, 3)) / 3, mean_places
    )
    result = {}
    va = n("rated_power") * 1000
    for i, (stage, tap) in enumerate(
        zip(("NTBT", "NTAT", "HTBT", "HTAT", "LTBT", "LTAT"), (1, 1, 1.05, 1.05, 0.9, 0.9))
    ):
        period = i % 2
        ih = va / (sqrt3 * n("rated_hv") * tap)
        il = va / (sqrt3 * n("rated_lv"))
        rh = avg(f"hv_resistance.{i}", "R")
        rl = avg(f"lv_resistance.{period}", "R") / 1000
        temp = n(f"lv_resistance.{period}.temperature")
        measured = _rounded(
            sum(n(f"load_measurement.{i}.P{j}") for j in (1, 2, 3))
            * (ih / avg(f"load_measurement.{i}", "I")) ** 2,
            test_places,
        )
        copper = _rounded(1.5 * (ih * ih * rh + il * il * rl), copper_places)
        factor = _rounded(310 / (235 + temp), factor_places)
        corrected = _rounded(copper * factor + (measured - copper) / factor, output_places)
        result[(stage, "total_loss_100")] = round(n(f"no_load.{period}.P") + corrected, 2)
        if i < 2:
            half = _rounded(
                n(f"half_load.{period}.P") * (ih * 0.5 / n(f"half_load.{period}.I")) ** 2,
                test_places,
            )
            half_copper = _rounded(copper * 0.25, copper_places)
            corrected_half = _rounded(
                half_copper * factor + (half - half_copper) / factor, output_places
            )
            result[(stage, "total_loss_50")] = round(n(f"no_load.{period}.P") + corrected_half, 2)
    return result


def search():
    baseline_rows = calculate_datasheet(VALUES)
    baseline = {
        (row["stage"], metric): round(row[metric], 2)
        for row in baseline_rows
        for metric in ("total_loss_50", "total_loss_100")
        if metric in row
    }
    choices = itertools.product(
        (sqrt(3), 1.73205, 1.732),
        (None, 4, 3, 2),
        (None, 3, 2, 1),
        (None, 3, 2, 1),
        (None, 5, 4, 3),
        (None, 3, 2),
    )
    best = None
    exact = []
    tried = 0
    for policy in choices:
        tried += 1
        try:
            actual = candidate(policy)
        except (ValueError, ZeroDivisionError):
            continue
        gaps = {key: round(actual[key] - target, 2) for key, target in PRINTED.items()}
        score = (sum(abs(gap) for gap in gaps.values()), max(abs(gap) for gap in gaps.values()))
        if best is None or score < best[0]:
            best = (score, policy, actual, gaps)
        if all(gap == 0 for gap in gaps.values()):
            exact.append(policy)
    return {
        "combinations_tried": tried,
        "grid": {
            "sqrt3": ["exact", "1.73205", "1.732"],
            "mean_dp": [None, 4, 3, 2],
            "measured_loss_dp": [None, 3, 2, 1],
            "copper_loss_dp": [None, 3, 2, 1],
            "temperature_factor_dp": [None, 5, 4, 3],
            "corrected_loss_dp": [None, 3, 2],
        },
        "exact_match_count": len(exact),
        "exact_policies": [list(item) for item in exact[:5]],
        "baseline_gap_w": {
            f"{stage} {metric}": round(baseline[(stage, metric)] - target, 2)
            for (stage, metric), target in PRINTED.items()
        },
        "closest_policy": list(best[1]),
        "closest_gap_w": {f"{stage} {metric}": gap for (stage, metric), gap in best[3].items()},
        "closest_values_w": {
            f"{stage} {metric}": value for (stage, metric), value in best[2].items()
        },
    }


if __name__ == "__main__":
    print(json.dumps(search(), indent=2))
