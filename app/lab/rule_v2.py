"""Team-reviewed v2 decision rules. V1 evaluation is deliberately untouched.

Every result remains provisional until CPRI adopts the relevant rule version.
Unknown standards, missing declarations and unclear observations block a verdict.
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .calculations import _derived_input, finite
from .principal_tap import nominal_phase_ratio, principal_row_prefix

REVIEW_STATUS = "reviewed by team, pending CPRI adoption"


def _field(fields, form_type, key, unit=None, optional=False):
    matches = [
        field
        for field in fields
        if field.get("form_type") == form_type and field.get("schema_key") == key
    ]
    if not matches and optional:
        return None
    if len(matches) != 1:
        raise ValueError(
            f"Expected one matching reading; found {len(matches)} for {form_type}.{key}."
        )
    field = matches[0]
    if field.get("status") != "verified":
        raise ValueError(f"Input needs review: {form_type}.{key}.")
    if unit is not None and field.get("unit", "") != unit:
        raise ValueError(f"Unit mismatch: {form_type}.{key}; expected {unit!r}.")
    if not str(field.get("value", "")).strip():
        raise ValueError(f"Missing value: {form_type}.{key}.")
    return field


def _derived(calculations, stage, metric, unit):
    return _derived_input(
        {"derived": {"form_type": "loss_calculation", "stage": stage, "metric": metric}},
        calculations,
        unit,
    )


def _number(field):
    return finite(field["value"], field["key"])


def _bound_result(result, value, lower=None, upper=None, unit="", quality_band=Decimal("2")):
    if lower is None and upper is None:
        raise ValueError("No acceptance boundary configured.")
    distances = []
    if lower is not None:
        distances.append((value - lower, lower, "lower"))
    if upper is not None:
        distances.append((upper - value, upper, "upper"))
    margin, boundary, side = min(distances, key=lambda row: row[0])
    passed = all(distance >= 0 for distance, _, _ in distances)
    near = passed and any(
        distance <= abs(limit) * quality_band / 100 for distance, limit, _ in distances
    )
    verdict = "marginal" if near else "pass" if passed else "fail"
    result.update(
        value=str(value),
        unit=unit,
        limit=str(boundary),
        margin=str(margin),
        margin_unit=unit,
        limit_side=side,
        verdict=verdict,
        state="within_limits" if passed else "outside_limits",
        quality_flag="near_limit" if near else "",
        message=(
            f"{verdict.upper()}: within {margin} {unit} of {side} limit {boundary} {unit}."
            if passed
            else f"FAIL: exceeded {side} limit {boundary} {unit} by {abs(margin)} {unit}."
        ),
    )


def _observed(text):
    words = re.findall(r"[a-z]+", str(text).casefold())
    tokens = set(words)
    if "not" in tokens and "withstood" in tokens:
        return "failed"
    if "failed" in tokens or "failure" in tokens or ("breakdown" in tokens and "no" not in tokens):
        return "failed"
    if tokens in (
        {"withstood"},
        {"satisfactory"},
        {"no", "breakdown"},
        {"no", "disruptive", "discharge", "withstood"},
        {"no", "leakage", "observed"},
        {"no", "leakage", "at", "any", "point"},
        {"no", "visible", "defects"},
    ):
        return "withstood"
    return "unrecognised"


def normalize_pressure(value, unit):
    """Normalise an explicitly recorded kPa pressure; never infer other units."""
    match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(kpa)?\s*", str(value), re.I)
    if not match or str(unit or "").casefold() not in ("", "kpa"):
        raise ValueError("Pressure unit requires source confirmation.")
    return f'{finite(match.group(1), "pressure")} kPa'


def evaluate_v2(rule, fields, calculations, context):
    params = rule.parameters
    kind = params["v2_kind"]
    result = {
        "code": rule.code,
        "version": 2,
        "title": rule.title,
        "operation": rule.operation,
        "parameters": params,
        "source_clause": rule.source_clause,
        "rule_status": rule.status,
        "test_type": params.get("test_type", ""),
        "v2_status": params.get("v2_status", "ACTIVE"),
        "provenance_status": REVIEW_STATUS,
        "state": "blocked",
        "verdict": "blocked",
        "value": None,
        "margin": None,
        "margin_unit": None,
        "inputs": [],
        "unit": params.get("unit", ""),
    }
    try:
        quality_band = finite(params.get("quality_margin_percent", 2), "internal Quality band")
        if not 0 <= quality_band <= 100:
            raise ValueError("Internal Quality band must be between 0 and 100%.")
        if kind == "not_applicable":
            if rule.code in ("LOSS_100_HTBT", "LOSS_100_HTAT", "LOSS_100_LTBT", "LOSS_100_LTAT"):
                actual = _derived(calculations, params["stage"], "total_loss_100", "W")
                result.update(
                    inputs=[actual],
                    value=actual["value"],
                    unit="W",
                    v2_status="DESCRIPTIVE",
                    state="descriptive",
                    verdict="descriptive",
                    message="Calculated high/low-tap total loss; no principal-tap EEL verdict assigned.",
                )
            else:
                result.update(
                    state="not_applicable", verdict="not_applicable", message=params["reason"]
                )
        elif kind == "eel_principal":
            stage, load = params["stage"], params["load"]
            actual = _derived(calculations, stage, "total_loss_" + load, "W")
            worksheet = _derived(calculations, stage, "limit_" + load, "W")
            rating = _field(fields, "loss_calculation", "rated_power")
            eel = _field(fields, "loss_calculation", "efficiency_level", "")
            result["inputs"] = [actual, worksheet, rating, eel]
            if rating.get("unit", "").casefold() != "kva":
                raise ValueError("Rated power must be reviewed in kVA.")
            key = (
                (
                    format(_number(rating), "f").rstrip("0").rstrip(".")
                    if "." in format(_number(rating), "f")
                    else format(_number(rating), "f")
                ),
                "EEL-" + re.sub(r"[^0-9]", "", eel["value"]),
            )
            table = params.get("eel_table", {}).get(key[0], {}).get(key[1])
            if not table:
                raise ValueError(
                    f"IS 1180-1 Amendment 4 EEL limit not configured for {key[0]} kVA / {key[1]}."
                )
            limit = finite(table[load], "EEL table limit")
            result["limit_provenance"] = (
                f"IS 1180 (Part 1):2014 incl. Amendment 4 table, {key[0]} kVA {key[1]}"
            )
            if _number(worksheet) != limit:
                raise ValueError("limit mismatch")
            _bound_result(result, _number(actual), upper=limit, unit="W", quality_band=quality_band)
        elif kind == "descriptive":
            metric, unit, stage = params["metric"], params["unit"], params["stage"]
            after = _derived(calculations, stage, metric, unit)
            before = _derived(calculations, stage.replace("AT", "BT"), metric, unit)
            result["inputs"] = [before, after]
            result.update(
                value=after["value"],
                before_value=before["value"],
                state="descriptive",
                verdict="descriptive",
                message=f'Before {before["value"]} {unit}; after {after["value"]} {unit}. No pass/fail assigned.',
            )
        elif kind == "impedance":
            stage = params["stage"]
            actual = _derived(calculations, stage, "Z75", "%")
            declared_key = (
                "impedance_at_75"
                if stage.startswith("NT")
                else (
                    "impedance_high_tap_at_75"
                    if stage.startswith("HT")
                    else "impedance_low_tap_at_75"
                )
            )
            declared = _field(
                fields,
                "transformer_proforma",
                declared_key,
                "%",
                optional=not stage.startswith("NT"),
            )
            result["inputs"] = [actual] + ([declared] if declared else [])
            if declared is None:
                result.update(
                    value=actual["value"],
                    unit="%",
                    v2_status="DESCRIPTIVE",
                    state="descriptive",
                    verdict="descriptive",
                    message="Calculated tap impedance; no tap-specific declaration for pass/fail.",
                )
            else:
                reference = _number(declared)
                if reference >= 10 or reference <= 0:
                    raise ValueError(
                        "This v2 impedance tolerance applies only to declared 0 < Z < 10%."
                    )
                percent = Decimal("10") if stage.startswith("NT") else Decimal("15")
                lower, upper = reference * (1 - percent / 100), reference * (1 + percent / 100)
                _bound_result(
                    result,
                    _number(actual),
                    lower=lower,
                    upper=upper,
                    unit="%",
                    quality_band=quality_band,
                )
                result["display_basis"] = f"{_number(actual):.3f}% within {lower:.2f}–{upper:.2f}%"
                result["limit_provenance"] = f"Declared {reference}%Z; ±{percent}% band; clause TBC"
        elif kind == "reactance":
            before_stage, after_stage = params["before"], params["after"]
            before = _derived(calculations, before_stage, "X50", "%")
            after = _derived(calculations, after_stage, "X50", "%")
            coil = _field(fields, "transformer_proforma", "circular", "")
            result["inputs"] = [before, after, coil]
            pre = _number(before).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            post = _number(after).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            if pre == 0:
                raise ValueError("Reactance baseline is zero.")
            signed = (pre - post) / pre * 100
            result.update(
                value=f"{signed:.3f}",
                signed_change=f"{signed:.3f}",
                unit="%",
                rounded_before=str(pre),
                rounded_after=str(post),
            )
            coil_value = coil["value"].strip().casefold()
            if coil_value in ("ticked", "checked", "yes", "true", "1"):
                _bound_result(
                    result, abs(signed), upper=Decimal("2.0"), unit="%", quality_band=quality_band
                )
                result["value"] = f"{signed:.3f}"
                result["message"] += " Signed change shown; absolute change compared with 2.0%."
            elif coil_value in ("not ticked", "not checked", "no", "false", "0"):
                result["limit"] = "TBC per IS 2026-5"
                raise ValueError("non-circular limit not confirmed")
            else:
                raise ValueError("Coil geometry requires source confirmation.")
        elif kind == "no_load":
            if rule.code.endswith("112"):
                requested = context.get("requested_tests_text")
                if not requested:
                    raise ValueError("CRF request for 112.5% no-load special test not confirmed.")
                if "112" not in str(requested):
                    result.update(
                        state="not_applicable",
                        verdict="not_applicable",
                        message="112.5% special test not requested in the CRF.",
                    )
                    return result
            key = "no_load_" + params["voltage"] + "_percent"
            measured = _field(fields, "loss_measurement", key, "%")
            result["inputs"] = [measured]
            _bound_result(
                result,
                _number(measured),
                upper=Decimal(str(params["upper"])),
                unit="%",
                quality_band=quality_band,
            )
        elif kind == "temperature":
            measured = _field(fields, "temperature_rise", params["key"], "K")
            rating = _field(fields, "loss_calculation", "rated_power")
            guarantee = _field(
                fields, "transformer_proforma", params["guarantee"], "K", optional=True
            )
            result["inputs"] = [measured, rating] + ([guarantee] if guarantee else [])
            if rating.get("unit", "").casefold() != "kva":
                raise ValueError("Rated power must be reviewed in kVA.")
            kva = _number(rating)
            band = "up_to_200" if kva <= 200 else "250_to_2500" if 250 <= kva <= 2500 else None
            if band is None:
                raise ValueError("IS 1180-1 temperature limit not configured for this kVA rating.")
            category = "top_oil" if params["key"] == "top_oil_rise" else "winding"
            configured = params.get("standard_limits", {}).get(band, {}).get(category)
            cited_source = params.get("standard_limits_source")
            if configured is None or not cited_source:
                raise ValueError("Cited IS 1180-1 temperature limit not configured.")
            standard = finite(configured, "configured IS 1180-1 temperature limit")
            declared = _number(guarantee) if guarantee else None
            limit = min(declared, standard) if declared is not None else standard
            result["limit_provenance"] = (
                f"manufacturer guarantee {declared} K (stricter than cited {cited_source} limit {standard} K)"
                if declared is not None and declared < standard
                else (
                    f"manufacturer guarantee {declared} K and cited {cited_source} limit {standard} K"
                    if declared is not None
                    else f"cited {cited_source} limit {standard} K; no guarantee printed"
                )
            )
            _bound_result(
                result, _number(measured), upper=limit, unit="K", quality_band=quality_band
            )
        elif kind in ("observation", "pressure_observation", "oil_observation"):
            observed = []
            observation_text = []
            for form_type, key in params["observation_fields"]:
                field = _field(fields, form_type, key, "")
                result["inputs"].append(field)
                observed.append(_observed(field["value"]))
                observation_text.append(field["value"])
            if kind == "pressure_observation":
                pressure = _field(fields, "pressure_oil_leakage", "routine_pressure", optional=True)
                if pressure:
                    result["inputs"].append(pressure)
                    result["normalised_pressure"] = normalize_pressure(
                        pressure["value"], pressure.get("unit", "")
                    )
            if kind == "oil_observation":
                pressure = next(
                    (
                        field
                        for field in fields
                        if field.get("form_type") == "pressure_oil_leakage"
                        and field.get("schema_key") == "oil_routine_pressure"
                    ),
                    None,
                )
                if pressure and pressure.get("status") == "verified":
                    pressure = _field(fields, "pressure_oil_leakage", "oil_routine_pressure")
                    result["inputs"].append(pressure)
                    result["normalised_pressure"] = normalize_pressure(
                        pressure["value"], pressure.get("unit", "")
                    )
            result["value"] = "; ".join(dict.fromkeys(observation_text))
            result["unit"] = ""
            result["observation"] = (
                "failed"
                if "failed" in observed
                else "withstood" if all(x == "withstood" for x in observed) else "unrecognised"
            )
            if result["observation"] == "failed":
                result.update(
                    state="outside_limits",
                    verdict="fail",
                    message="Recorded observation indicates failure; engineer review required.",
                )
            elif result["observation"] == "withstood":
                result.update(
                    state="observed",
                    verdict="pass",
                    message="Recorded observation normalised to withstood; engineer confirmation required.",
                )
            else:
                raise ValueError("Unrecognised observation wording requires engineer confirmation.")
        elif kind == "ratio":
            nominal, nameplate = nominal_phase_ratio(fields)
            prefix = principal_row_prefix(fields, "routine_test", "ratio", nominal_ratio=nominal)
            values = {
                stage: [
                    _field(fields, "routine_test", f"{prefix}.{stage}_{phase}") for phase in "ABC"
                ]
                for stage in ("BT", "AT")
            }
            impedance = _derived(calculations, "NTBT", "Z75", "%")
            tap_field = _field(fields, "routine_test", prefix + ".tap")
            result["inputs"] = [*nameplate, tap_field, *values["BT"], *values["AT"], impedance]
            means = {}
            for stage in ("BT", "AT"):
                numbers = [_number(field) for field in values[stage]]
                if min(numbers) <= 0 or max(numbers) / min(numbers) > Decimal("1.05"):
                    raise ValueError(
                        f"Principal-tap {stage} ratio phases disagree; source check required."
                    )
                means[stage] = sum(numbers) / 3
            allowed = min(
                finite(params["ratio_max_percent"], "configured ratio tolerance"),
                _number(impedance)
                * finite(params["ratio_impedance_fraction"], "configured impedance fraction"),
            )
            if allowed <= 0:
                raise ValueError("Configured ratio tolerance must be positive.")
            deviations = {stage: (means[stage] - nominal) / nominal * 100 for stage in ("BT", "AT")}
            phase_deviations = [
                (stage, phase, field, (_number(field) - nominal) / nominal * 100)
                for stage in ("BT", "AT")
                for phase, field in zip("ABC", values[stage])
            ]
            worst_stage, worst_phase, worst_field, worst = max(
                phase_deviations, key=lambda item: abs(item[3])
            )
            result["limit_provenance"] = (
                f'Lower of ±{params["ratio_max_percent"]}% and {params["ratio_impedance_fraction"]} × measured %Z: ±{allowed}% [CLAUSE TBC]'
            )
            _bound_result(result, abs(worst), upper=allowed, unit="%", quality_band=quality_band)
            result.update(
                nominal_ratio=str(nominal),
                tap_label=tap_field["value"],
                before_ratio=str(means["BT"]),
                after_ratio=str(means["AT"]),
                before_deviation=str(deviations["BT"]),
                after_deviation=str(deviations["AT"]),
                worst_phase=f"{worst_stage} {worst_phase}",
                worst_phase_ratio=worst_field["value"],
                worst_phase_deviation=str(worst),
                phase_deviations={
                    f"{stage} {phase}": str(deviation)
                    for stage, phase, _, deviation in phase_deviations
                },
            )
        elif kind == "sc_overall":
            prior = {item["code"]: item for item in context.get("prior_results", [])}
            required = ("REACTANCE_NTBT_NTAT", "REACTANCE_HTBT_HTAT", "REACTANCE_LTBT_LTAT")
            if any(
                prior.get(code, {}).get("verdict") not in ("pass", "marginal") for code in required
            ):
                raise ValueError("R15–R17 reactance checks have not all passed.")
            for key in ("during_test", "after_test"):
                field = _field(fields, "short_circuit", key, "")
                result["inputs"].append(field)
                if field["value"].strip().casefold() != "no abnormalities":
                    raise ValueError(
                        f"Short-circuit {key} observation requires engineer interpretation."
                    )
            for key, allowed in (
                ("conductor_core_clamps", {"no visible damage", "no visible defects", "intact"}),
                ("spacers", {"intact", "no visible damage", "no visible defects"}),
                ("oil", {"clean", "no contamination", "clear"}),
            ):
                field = _field(fields, "short_circuit", key, "")
                result["inputs"].append(field)
                if field["value"].strip().casefold() not in allowed:
                    raise ValueError(
                        f"Untanking inspection {key} requires engineer interpretation."
                    )
            result.update(
                state="observed_pass",
                verdict="pass",
                v2_status="OBSERVATION",
                value="No abnormalities during/after; untanking satisfactory",
                unit="",
                message="Reactance checks passed; checked during/after and untanking observations are satisfactory.",
            )
        else:
            raise ValueError("Unsupported v2 rule kind.")
    except (KeyError, ValueError, TypeError, InvalidOperation, ZeroDivisionError) as error:
        result.update(state="blocked", verdict="blocked", margin=None, message=str(error))
    if rule.status != "confirmed":
        result["message"] = "PROVISIONAL RULE: " + result["message"]
    return result
