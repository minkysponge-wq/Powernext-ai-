"""Station test types are selected by configuration, not Python branches."""

import json
from functools import lru_cache
from pathlib import Path

from .extraction import schemas


@lru_cache(maxsize=1)
def station_types():
    data = json.loads(Path(__file__).with_name("station_types.json").read_text(encoding="utf-8"))
    known = {s["form_type"]: {f["key"] for f in s["fields"]} for s in schemas()}
    if not isinstance(data, list) or any(
        not isinstance(x, dict)
        or x.get("form_type") not in known
        or not isinstance(x.get("label"), str)
        or not x["label"]
        for x in data
    ):
        raise ValueError("Station catalog must reference configured form schemas.")
    if len({x["form_type"] for x in data}) != len(data):
        raise ValueError("Duplicate station test type.")
    for item in data:
        required = item.get("lock_required")
        results = item.get("result_any_of")
        keys = known[item["form_type"]]
        if (
            not isinstance(required, list)
            or not required
            or not isinstance(results, list)
            or not results
            or not all(isinstance(key, str) and key in keys for key in required + results)
            or len(set(required)) != len(required)
            or len(set(results)) != len(results)
        ):
            raise ValueError(
                "Station lock requirements must name fields in the matching form schema."
            )
    return data


def station_type(form_type):
    return next(item for item in station_types() if item["form_type"] == form_type)
