"""Deterministic, evidence-linked analysis of frozen report snapshots.

No generative text or inferred measurements enter this module. A numeric value
comes from a reviewed field, a recorded rule result, or the worksheet engine.
"""

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation


def _number(value):
    try:
        result = Decimal(str(value).strip())
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _shown(value, places=3):
    number = _number(value)
    if number is None:
        return str(value)
    return format(number.quantize(Decimal(1).scaleb(-places)), "f").rstrip("0").rstrip(".") or "0"


def analysis(snapshot):
    documents = {item["id"]: item["name"] for item in snapshot.get("documents", [])}
    fields = snapshot.get("fields", [])
    sections = defaultdict(list)

    def source(field):
        name = (
            documents.get(field.get("document_id"))
            or field.get("source")
            or "Digital station entry"
        )
        page = field.get("page") or field.get("schema_page")
        return name + (f", page {page}" if page and field.get("document_id") else "")

    def add(kind, text, references):
        references = list(dict.fromkeys(str(ref) for ref in references if ref))
        sections[kind].append({"text": text, "references": references})

    def reviewed(kind, key):
        matches = [
            field
            for field in fields
            if field.get("form_type") == kind
            and field.get("schema_key") == key
            and field.get("status") == "verified"
            and str(field.get("value", "")).strip()
        ]
        return matches[0] if len(matches) == 1 else None

    verdicts = snapshot.get("calculations", [])
    for item in verdicts:
        kind = item.get("test_type") or "other"
        reference = f"{item.get('title', 'Engineering check')}: {item.get('source_clause') or 'clause unconfirmed'}"
        evidence = [
            source(field)
            for field in item.get("inputs", [])
            if field.get("document_id") or field.get("source")
        ]
        state = item.get("verdict", "blocked")
        if state == "blocked":
            text = f"{item.get('title', 'Check')}: no verdict. Required reviewed evidence or rule configuration is missing."
        elif state == "not_applicable":
            text = f"{item.get('title', 'Check')}: not applicable under its recorded condition."
        elif state == "not_configured":
            text = (
                f"{item.get('title', 'Check')}: calculated, but no acceptance limit is configured."
            )
        elif item.get("operation") == "all_text":
            text = f"{item.get('title', 'Check')}: {state}; required recorded observations: {item.get('value', '')}."
        else:
            text = (
                f"{item.get('title', 'Check')}: {state}; measured {_shown(item.get('value'))} {item.get('unit', '')}; "
                f"{item.get('limit_side', '')} limit {_shown(item.get('limit'))} {item.get('margin_unit', '')}; "
                f"signed margin {_shown(item.get('margin'))} {item.get('margin_unit', '')}."
            )
        if item.get("rule_status") != "confirmed":
            text += " Rule remains provisional."
        add(kind, text, [reference, *evidence])

    calculations = [
        item for item in snapshot.get("transformer_calculations", []) if not item.get("error")
    ]
    if calculations:
        for calculation in calculations:
            reference = calculation.get("source") or "Reviewed loss worksheet"
            for row in calculation.get("rows", []):
                stage = row.get("stage", "Unknown tap")
                parts = [
                    f"{stage}: load loss at 75 C {_shown(row['load_loss_100'])} W",
                    f"stray component {_shown(row['stray_loss'])} W",
                    f"stray share {_shown(row.get('stray_share_percent'))}%",
                ]
                for fraction in ("50", "100"):
                    if "total_loss_" + fraction in row and "limit_" + fraction in row:
                        total, limit = row["total_loss_" + fraction], row["limit_" + fraction]
                        parts.append(
                            f"{fraction}% total {_shown(total)} W vs {_shown(limit)} W; "
                            f"margin {_shown(Decimal(str(limit))-Decimal(str(total)))} W"
                        )
                add(
                    "loss_calculation",
                    "; ".join(parts) + ".",
                    [
                        reference,
                        "Worksheet correction: R75 = Rt x 310/(235+t) for copper; load loss separates copper and stray components",
                    ],
                )
                add(
                    "loss_calculation",
                    f"{stage}: corrected impedance at 75 C {_shown(row['Z75'])}%; reactance component {_shown(row['X50'])}%.",
                    [reference, "Worksheet impedance-at-75-C calculation"],
                )
    else:
        add(
            "loss_calculation",
            "Per-tap loss, stray share and 75 C impedance analysis withheld until the worksheet inputs are reviewed.",
            ["Configured loss-calculation worksheet"],
        )

    for fraction, label in (("100", "100%"), ("112", "112.5%")):
        observed, limit = (
            reviewed("loss_measurement", "no_load_" + fraction + suffix)
            for suffix in ("_percent", "_limit_percent")
        )
        current = reviewed("loss_measurement", "no_load_" + fraction + "_current")
        if observed and limit:
            text = (
                f"No-load current at {label} voltage: "
                + (
                    f"{current['value']} {current.get('unit','')} "
                    if current
                    else "current in amperes not recorded; "
                )
                + f"{observed['value']}% versus recorded limit {limit['value']}%; engineering verdict is shown under the linked rule."
            )
            add(
                "loss_measurement",
                text,
                [source(field) for field in (observed, limit, current) if field],
            )
        else:
            add(
                "loss_measurement",
                f"No-load current at {label} voltage: reviewed reading or recorded limit missing; comparison withheld.",
                ["Configured loss-measurement fields"],
            )
    no_load_loss = reviewed("loss_calculation", "no_load.0.P") or reviewed(
        "loss_measurement", "no_load.0.Wtotal"
    )
    if no_load_loss:
        add(
            "loss_measurement",
            f"No-load loss at rated voltage: {no_load_loss['value']} {no_load_loss.get('unit','')}.",
            [source(no_load_loss)],
        )
    else:
        add(
            "loss_measurement",
            "No-load loss at rated voltage: reviewed result not recorded.",
            ["Configured loss worksheet"],
        )

    for winding, count in (("hv", 3), ("lv", 1)):
        for tap in range(count):
            for stage in ("BT", "AT"):
                keys = [f"{winding}_resistance.{tap}.{stage}_{phase}" for phase in (1, 2, 3)]
                phases = [reviewed("loss_measurement", key) for key in keys]
                values = [_number(field["value"]) if field else None for field in phases]
                if all(value is not None for value in values):
                    mean = sum(values) / Decimal(3)
                    imbalance = (max(values) - min(values)) / mean * 100 if mean else None
                    if imbalance is not None:
                        add(
                            "routine_test",
                            f"{winding.upper()} winding resistance, tap {tap+1} {stage}: phase mean {_shown(mean, 4)} {phases[0].get('unit','')}; phase spread {_shown(imbalance, 2)}% of the mean.",
                            [source(field) for field in phases],
                        )

    for kind, key, label in (
        ("temperature_rise", "top_oil_rise", "Top-oil rise"),
        ("temperature_rise", "hv_winding_rise", "HV winding rise"),
        ("temperature_rise", "lv_winding_rise", "LV winding rise"),
    ):
        field = reviewed(kind, key)
        if field:
            add(
                kind,
                f"{label}: recorded {field['value']} {field.get('unit', '')}; the applicable guaranteed-margin verdict is shown under its linked rule.",
                [source(field)],
            )
        else:
            add(
                kind,
                f"{label}: reviewed result missing; comparison withheld.",
                ["Configured temperature-rise field"],
            )
    time_points = [
        field
        for field in fields
        if field.get("form_type") == "temperature_rise"
        and re.fullmatch(
            r"time_series\.\d+\.(hour|time|top_oil|bottom_oil)", field.get("schema_key", "")
        )
        and field.get("status") == "verified"
        and str(field.get("value", "")).strip()
    ]
    if time_points:
        add(
            "temperature_rise",
            f"Heating curve: {len(time_points)} reviewed time/temperature cells are plotted in the source table. Steady-state time is not derived without a configured criterion.",
            [source(field) for field in time_points],
        )
    else:
        add(
            "temperature_rise",
            "Heating curve and steady-state time: reviewed series missing; analysis withheld.",
            ["Configured temperature-rise time-series fields"],
        )
    add(
        "temperature_rise",
        "Winding-rise formula check: no separately configured resistance-to-rise rule; the recorded rise and guaranteed-limit verdict remain separate.",
        ["Configured temperature-rise rule set"],
    )

    for prefix in ("ir_hv_earth", "ir_lv_earth", "ir_hv_lv"):
        before, after = (reviewed("routine_test", prefix + suffix) for suffix in ("_BT", "_AT"))
        label = prefix.replace("_", " ").upper()
        if before and after:
            add(
                "routine_test",
                f"{label}: before {before['value']} {before.get('unit', '')}; after {after['value']} {after.get('unit', '')}. No IR acceptance threshold is configured.",
                [source(before), source(after)],
            )
        else:
            add(
                "routine_test",
                f"{label}: before/after reviewed readings incomplete; comparison withheld.",
                ["Configured routine-test IR fields"],
            )
    for label, keys in (
        ("Induced dielectric", ("induced_observation_BT", "induced_observation_AT")),
        ("Separate-source HV dielectric", ("hv_observation_BT", "hv_observation_AT")),
        ("Separate-source LV dielectric", ("lv_observation_BT", "lv_observation_AT")),
    ):
        observed = [reviewed("routine_test", key) for key in keys]
        if all(observed):
            add(
                "routine_test",
                f"{label}: before {observed[0]['value']}; after {observed[1]['value']}. Acceptance follows the configured observation rule only.",
                [source(field) for field in observed],
            )
        else:
            add(
                "routine_test",
                f"{label}: before/after observation incomplete; conclusion withheld.",
                ["Configured dielectric-observation fields"],
            )
    for index in range(7):
        tap = reviewed("routine_test", f"ratio.{index}.tap")
        if not tap:
            continue
        readings = [
            reviewed("routine_test", f"ratio.{index}.{part}")
            for part in ("BT_A", "BT_B", "BT_C", "AT_A", "AT_B", "AT_C")
        ]
        if all(readings):
            add(
                "routine_test",
                f"Ratio tap {tap['value']}: before/after phase readings are recorded. Percentage deviation versus nominal remains withheld until a nominal ratio rule is configured for this tap.",
                [source(tap), *[source(field) for field in readings]],
            )
        else:
            add(
                "routine_test",
                f"Ratio tap {tap['value']}: reviewed phase readings incomplete; deviation comparison withheld.",
                [source(tap)],
            )

    for index in range(11):
        peak = reviewed("short_circuit", f"shots.{index}.peak")
        rms = reviewed("short_circuit", f"shots.{index}.rms_avg")
        if peak or rms:
            parts = [f"Shot {index+1}"]
            if rms:
                parts.append(f"RMS {rms['value']} {rms.get('unit', '')}")
            if peak:
                parts.append(f"peak {peak['value']} {peak.get('unit', '')}")
            tap = reviewed("short_circuit", f"shots.{index}.tap")
            category = (
                {"NT": "normal", "HT": "high", "LT": "low"}.get(tap["value"].strip().upper())
                if tap
                else None
            )
            required = (
                [
                    reviewed("short_circuit", f"required_{category}_{suffix}")
                    for suffix in ("rms", "peak")
                ]
                if category
                else [None, None]
            )
            if all(required) and rms and peak:
                parts.append(f"required RMS {required[0]['value']} {required[0].get('unit','')}")
                parts.append(f"required peak {required[1]['value']} {required[1].get('unit','')}")
                parts.append("acceptance follows the configured shot rule")
            else:
                parts.append(
                    "required-versus-achieved comparison withheld: tap-duty mapping or reviewed limit missing"
                )
            add(
                "short_circuit",
                "; ".join(parts) + ".",
                [source(field) for field in (rms, peak, tap, *required) if field],
            )
    for key in (
        "condition_before",
        "during_test",
        "after_test",
        "conductor_core_clamps",
        "spacers",
        "oil",
    ):
        field = reviewed("short_circuit", key)
        if field:
            add(
                "short_circuit",
                f"Inspection {key.replace('_',' ')}: {field['value']}.",
                [source(field)],
            )
    if not sections["short_circuit"]:
        add(
            "short_circuit",
            "Shot and inspection analysis withheld; reviewed evidence missing.",
            ["Configured short-circuit logsheet"],
        )
    for index in range(8):
        point = reviewed("pressure_oil_leakage", f"deflection.{index}.point")
        difference = reviewed("pressure_oil_leakage", f"deflection.{index}.difference")
        if point and difference:
            add(
                "pressure_oil_leakage",
                f"Deflection point {point['value']}: recorded difference {difference['value']} {difference.get('unit','')}; permissible comparison withheld unless a point-specific limit rule is configured.",
                [source(point), source(difference)],
            )
    for key in ("routine_pressure_observation", "type_observation", "oil_observation"):
        field = reviewed("pressure_oil_leakage", key)
        if field:
            add(
                "pressure_oil_leakage",
                f"{key.replace('_',' ').title()}: {field['value']}.",
                [source(field)],
            )
    if not sections["pressure_oil_leakage"]:
        add(
            "pressure_oil_leakage",
            "Pressure, deflection and oil-leakage analysis withheld; reviewed evidence missing.",
            ["Configured pressure and oil-leakage form"],
        )

    for finding in snapshot.get("findings", []):
        if finding.get("id", "").startswith(
            ("duplicate-", "rating-", "arithmetic-", "request-mismatch-")
        ):
            refs = [
                source(field) for field in fields if field.get("id") in finding.get("evidence", [])
            ]
            add(
                "data_quality",
                f"{finding['title']}: {finding['detail']}",
                refs or ["Frozen validation finding " + finding["id"]],
            )
    unreviewed = sum(field.get("status") in ("unreviewed", "ambiguous") for field in fields)
    if unreviewed:
        add(
            "data_quality",
            f"{unreviewed} readings remain unreviewed or ambiguous; no conformity conclusion is issued from them.",
            ["Frozen field review states"],
        )

    counts = snapshot.get("verdict_summary", {})
    if not verdicts:
        overall = "No engineering rules are assigned; overall conformity cannot be concluded."
    elif counts.get("fail", 0):
        overall = "At least one configured engineering check failed; overall conformity is not established."
    elif counts.get("blocked", 0) or counts.get("not_configured", 0) or unreviewed:
        overall = "Engineering checks remain blocked, lack a configured limit, or readings are unreviewed; overall conformity is withheld."
    elif all(item.get("rule_status") == "confirmed" for item in verdicts):
        overall = "All assigned engineering checks have results; Quality must approve the evidence before issue."
    else:
        overall = "All computed checks are shown, but provisional rules prevent a confirmed overall conclusion."
    return {
        "overall": {
            "text": overall,
            "references": [item.get("title") for item in verdicts] or ["Rule register"],
        },
        "sections": dict(sections),
    }
