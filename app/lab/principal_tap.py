"""Resolve the principal winding tap from recorded labels and nameplate data."""

import re
from decimal import Decimal, InvalidOperation


def field_by_key(fields, form_type, key, require_verified=True):
    matches = [
        item
        for item in fields
        if item.get("form_type") == form_type
        and item.get("schema_key") == key
        and not item.get("preview_excluded")
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected one {form_type}.{key} reading; found {len(matches)}.")
    item = matches[0]
    if require_verified and item.get("status") != "verified":
        raise ValueError(f"Input needs review: {form_type}.{key}.")
    if not str(item.get("value") or "").strip():
        raise ValueError(f"Missing value: {form_type}.{key}.")
    return item


def _numeric_principal_label(fields, require_verified):
    span = field_by_key(fields, "transformer_proforma", "tap_range", require_verified)["value"]
    step = field_by_key(fields, "transformer_proforma", "tap_step", require_verified)["value"]
    start = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", str(span))
    increment = re.search(r"(\d+(?:\.\d+)?)\s*%", str(step))
    if not start or not increment:
        raise ValueError("Nameplate tap schedule requires engineer confirmation.")
    try:
        offset = Decimal(start.group(1)) / Decimal(increment.group(1))
    except (InvalidOperation, ZeroDivisionError) as exc:
        raise ValueError("Invalid nameplate tap schedule.") from exc
    if offset != int(offset) or offset < 0:
        raise ValueError("Principal tap is not present in the nameplate schedule.")
    return str(1 + int(offset))


def principal_row_prefix(fields, form_type, family, require_verified=True, nominal_ratio=None):
    """Return a row prefix after matching its `.tap` VALUE, never its array index."""
    rows = []
    pattern = re.compile(r"^" + re.escape(family) + r"\.(\d+)\.tap$")
    for item in fields:
        if item.get("form_type") != form_type or not pattern.fullmatch(item.get("schema_key", "")):
            continue
        if item.get("preview_excluded"):
            continue
        if require_verified and item.get("status") != "verified":
            continue
        label = str(item.get("value") or "").strip().casefold()
        if label:
            rows.append((item["schema_key"].rsplit(".", 1)[0], label))
    if not rows:
        raise ValueError(f"No reviewed {family} tap labels recorded.")
    named = [prefix for prefix, label in rows if label in ("n", "nt", "normal", "principal")]
    if named:
        matches = named
    else:
        try:
            expected = _numeric_principal_label(fields, require_verified)
            matches = [prefix for prefix, label in rows if label == expected]
        except ValueError:
            if nominal_ratio is None or form_type != "routine_test" or family != "ratio":
                raise
            # A reviewed tap label and all six reviewed phase ratios may identify
            # the nominal position even while the nameplate schedule awaits review.
            matches = []
            for prefix, _ in rows:
                values = []
                try:
                    for stage in ("BT", "AT"):
                        for phase in "ABC":
                            values.append(
                                Decimal(
                                    str(
                                        field_by_key(
                                            fields,
                                            form_type,
                                            f"{prefix}.{stage}_{phase}",
                                            require_verified,
                                        )["value"]
                                    )
                                )
                            )
                except (ValueError, InvalidOperation):
                    continue
                if min(values) > 0 and all(
                    abs(value / Decimal(str(nominal_ratio)) - 1) <= Decimal("0.005")
                    for value in values
                ):
                    matches.append(prefix)
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one principal {family} tap label; found {len(matches)}."
        )
    return matches[0]


def nominal_phase_ratio(fields, require_verified=True):
    """For Dyn, HV delta phase voltage is HV line; LV star phase is LV line/√3."""

    def voltage(key):
        try:
            return field_by_key(fields, "transformer_proforma", key, require_verified)
        except ValueError:
            # The reviewed calculation datasheet repeats the same nameplate
            # voltages. Never read an unreviewed proforma prefill as evidence.
            return field_by_key(fields, "loss_calculation", key, require_verified)

    hv, lv = voltage("rated_hv"), voltage("rated_lv")
    group = field_by_key(fields, "transformer_proforma", "vector_group", require_verified)
    if hv.get("unit") != "V" or lv.get("unit") != "V":
        raise ValueError("Nameplate HV/LV units must be confirmed as V.")
    if not re.fullmatch(r"dyn\s*\d+", str(group["value"]).strip(), re.I):
        raise ValueError("Nameplate vector group is not a configured Dyn phase-ratio profile.")
    rated_hv, rated_lv = Decimal(str(hv["value"])), Decimal(str(lv["value"]))
    if rated_hv <= 0 or rated_lv <= 0:
        raise ValueError("Nameplate voltages must be positive.")
    return rated_hv * Decimal(3).sqrt() / rated_lv, [hv, lv, group]
