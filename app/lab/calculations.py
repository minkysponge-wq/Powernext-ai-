"""Safe, versioned engineering comparisons over reviewed evidence.

Rules are data, never Python expressions. A missing, duplicate, unreviewed or
unit-mismatched input withholds the verdict instead of being interpreted as a pass.
"""

from decimal import Decimal, InvalidOperation

OPERATIONS = {
    "identity",
    "maximum",
    "minimum",
    "difference",
    "ratio",
    "percent_deviation",
    "absolute_percent_change",
    "all_text",
}
DERIVED_UNITS = {
    "total_loss_50": "W",
    "total_loss_100": "W",
    "limit_50": "W",
    "limit_100": "W",
    "Z75": "%",
    "X50": "%",
    "load_loss_100": "W",
}


def finite(value, label):
    number = Decimal(str(value).strip())
    if not number.is_finite():
        raise ValueError(f"Non-finite {label}.")
    return number


def _field_input(selector, fields, unit):
    if not isinstance(selector, dict):
        raise ValueError("Each input must be a selector object.")
    if "key" in selector:
        matches = [field for field in fields if field["key"] == selector["key"]]
    elif "form_type" in selector and "schema_key" in selector:
        matches = [
            field
            for field in fields
            if field.get("form_type") == selector["form_type"]
            and field.get("schema_key") == selector["schema_key"]
        ]
    else:
        raise ValueError("Input needs an exact key or form_type and schema_key.")
    if len(matches) != 1:
        raise ValueError(f"Expected one matching reading; found {len(matches)} for {selector}.")
    field = matches[0]
    if field.get("status") != "verified":
        raise ValueError(f"Input needs review: {field.get('key')}.")
    if field.get("unit", "") != unit:
        raise ValueError(f"Unit mismatch: {field.get('key')}; expected {unit!r}.")
    if not str(field.get("value", "")).strip():
        raise ValueError(f"Missing value: {field.get('key')}.")
    return field


def _derived_input(selector, calculations, unit):
    config = selector.get("derived")
    if not isinstance(config, dict) or not all(
        config.get(k) for k in ("form_type", "stage", "metric")
    ):
        raise ValueError("Derived input needs form_type, stage and metric.")
    if config["form_type"] != "loss_calculation" or DERIVED_UNITS.get(config["metric"]) != unit:
        raise ValueError("Unsupported derived metric or unit.")
    matches = []
    for calculation in calculations or []:
        if calculation.get("form_type") != config["form_type"] or calculation.get("error"):
            continue
        for row in calculation.get("rows", []):
            if row.get("stage") == config["stage"] and config["metric"] in row:
                matches.append((calculation, row))
    if len(matches) != 1:
        raise ValueError(
            f"Expected one reviewed derived result; found {len(matches)} for {config}."
        )
    calculation, row = matches[0]
    return {
        "key": f"{config['form_type']}.{config['stage']}.{config['metric']}",
        "value": str(row[config["metric"]]),
        "unit": unit,
        "status": "verified",
        "source": calculation.get("source", "Reviewed calculation"),
        "document_id": calculation.get("document_id"),
    }


def _bound(params, name, values):
    constant, reference = name in params, name + "_input" in params
    if constant and reference:
        raise ValueError(f"Use either {name} or {name}_input, not both.")
    if reference:
        index = params[name + "_input"]
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= len(values)
        ):
            raise ValueError(f"Invalid {name}_input index.")
        return values[index]
    return finite(params[name], name + " limit") if constant else None


def evaluate_rule(rule, fields, calculations=None, context=None):
    """Return a measured value, explicit verdict and signed nearest-limit margin."""
    if rule.version == 2 and isinstance(rule.parameters, dict) and rule.parameters.get("v2_kind"):
        from .rule_v2 import evaluate_v2

        return evaluate_v2(rule, fields, calculations, context or {})
    result = {
        "code": rule.code,
        "version": rule.version,
        "title": rule.title,
        "operation": rule.operation,
        "parameters": rule.parameters,
        "source_clause": rule.source_clause,
        "rule_status": rule.status,
        "test_type": (
            rule.parameters.get("test_type", "") if isinstance(rule.parameters, dict) else ""
        ),
        "state": "blocked",
        "verdict": "blocked",
        "value": None,
        "margin": None,
        "margin_unit": None,
        "inputs": [],
    }
    try:
        params = rule.parameters
        if not isinstance(params, dict) or rule.operation not in OPERATIONS:
            raise ValueError("Unsupported operation or rule configuration.")
        unit = params["unit"]
        if not isinstance(unit, str):
            raise ValueError(
                "Specify the expected unit, including an empty string for dimensionless inputs."
            )
        if rule.status == "confirmed" and not rule.source_clause.strip():
            raise ValueError("Confirmed rules require a source clause.")
        conditions = params.get("conditions", [])
        if not isinstance(conditions, list):
            raise ValueError("Conditions must be a list.")
        for condition in conditions:
            if not isinstance(condition, dict) or not isinstance(condition.get("selector"), dict):
                raise ValueError("Each condition needs a field selector.")
            field = _field_input(condition["selector"], fields, condition.get("unit", ""))
            result["inputs"].append(dict(field))
            observed = field["value"].strip().casefold()
            accepted = {
                str(value).strip().casefold() for value in condition.get("accepted_values", [])
            }
            excluded = {
                str(value).strip().casefold() for value in condition.get("excluded_values", [])
            }
            if not accepted or accepted & excluded:
                raise ValueError("Condition needs distinct accepted and excluded values.")
            if observed in excluded:
                result.update(
                    state="not_applicable",
                    verdict="not_applicable",
                    message="Configured applicability condition does not apply.",
                )
                return result
            if observed not in accepted:
                raise ValueError("Applicability condition is unclear; review the source.")
        if "inputs" in params:
            selectors = params["inputs"]
        else:
            keys = params["fields"]
            selectors = [{"key": key} for key in keys]
        if not isinstance(selectors, list) or not selectors or len(selectors) > 20:
            raise ValueError("Provide 1–20 input selectors.")
        if len({str(selector) for selector in selectors}) != len(selectors):
            raise ValueError("Specify distinct input selectors.")
        values = []
        for selector in selectors:
            field = (
                _derived_input(selector, calculations, unit)
                if isinstance(selector, dict) and "derived" in selector
                else _field_input(selector, fields, unit)
            )
            result["inputs"].append(dict(field))
            if rule.operation != "all_text":
                values.append(finite(field["value"], "input " + str(field["key"])))
        if rule.operation == "all_text":
            accepted = params.get("accepted_values")
            if (
                not isinstance(accepted, list)
                or not accepted
                or not all(isinstance(x, str) and x.strip() for x in accepted)
            ):
                raise ValueError("Text verdict needs explicit accepted_values.")
            rejected = params.get("rejected_values", [])
            if not isinstance(rejected, list) or not all(
                isinstance(x, str) and x.strip() for x in rejected
            ):
                raise ValueError("rejected_values must be a list of non-empty text.")
            accepted_set = {x.strip().casefold() for x in accepted}
            rejected_set = {x.strip().casefold() for x in rejected}
            if accepted_set & rejected_set:
                raise ValueError("Accepted and rejected observations must be distinct.")
            observed = [field["value"].strip().casefold() for field in result["inputs"]]
            result.update(value="; ".join(field["value"] for field in result["inputs"]), unit="")
            if any(value in rejected_set for value in observed):
                result.update(
                    state="outside_limits",
                    verdict="fail",
                    message="A required observation matches configured rejection text.",
                )
            elif all(value in accepted_set for value in observed):
                result.update(
                    state="within_limits",
                    verdict="pass",
                    message="All required observations match configured acceptance text.",
                )
            else:
                result.update(
                    state="needs_review",
                    verdict="blocked",
                    message="Observation wording is unrecognised; review the source and decision rule.",
                )
        else:
            if rule.operation == "identity" and values:
                value = values[0]
            elif rule.operation == "maximum":
                value = max(values)
            elif rule.operation == "minimum":
                value = min(values)
            elif rule.operation == "difference" and len(values) == 2:
                value = values[0] - values[1]
            elif rule.operation == "ratio" and len(values) == 2:
                if values[1] == 0:
                    raise ValueError("Ratio denominator is zero.")
                value = values[0] / values[1]
            elif rule.operation == "percent_deviation" and len(values) == 2:
                if values[1] == 0:
                    raise ValueError("Nominal value is zero.")
                value = (values[0] - values[1]) / abs(values[1]) * 100
            elif rule.operation == "absolute_percent_change" and len(values) == 2:
                if values[0] == 0:
                    raise ValueError("Baseline value is zero.")
                value = abs((values[1] - values[0]) / values[0]) * 100
            else:
                raise ValueError("Wrong input count for operation.")
            output_unit = (
                "%"
                if rule.operation in ("percent_deviation", "absolute_percent_change")
                else "" if rule.operation == "ratio" else unit
            )
            lower, upper = _bound(params, "lower", values), _bound(params, "upper", values)
            tolerance = finite(params.get("tolerance", 0), "tolerance")
            band = finite(params.get("marginal_percent", 0), "marginal band")
            if tolerance < 0 or band < 0 or band > 100:
                raise ValueError(
                    "Tolerance and marginal band must be nonnegative; marginal_percent at most 100."
                )
            if lower is not None and upper is not None and lower > upper:
                raise ValueError("Lower tolerance exceeds upper tolerance.")
            result.update(value=str(value), unit=output_unit)
            if lower is None and upper is None:
                result.update(
                    state="calculated",
                    verdict="not_configured",
                    message="Calculated; no acceptance criterion configured.",
                )
            else:
                distances = []
                if lower is not None:
                    distances.append((value - (lower - tolerance), lower, "lower"))
                if upper is not None:
                    distances.append(((upper + tolerance) - value, upper, "upper"))
                margin, boundary, side = min(distances, key=lambda x: x[0])
                passed = all(distance >= 0 for distance, _, _ in distances)
                near_limit = passed and any(
                    distance <= abs(limit) * band / 100 for distance, limit, _ in distances
                )
                verdict = "marginal" if near_limit else "pass" if passed else "fail"
                result.update(
                    state="within_limits" if passed else "outside_limits",
                    verdict=verdict,
                    margin=str(margin),
                    margin_unit=output_unit,
                    limit=str(boundary),
                    limit_side=side,
                    message=f"{verdict.title()}: signed margin {margin} {output_unit} to configured {side} limit {boundary} {output_unit}.",
                )
        if rule.status != "confirmed":
            result["message"] = "PROVISIONAL RULE: " + result["message"]
    except (KeyError, TypeError, ValueError, InvalidOperation, ZeroDivisionError) as error:
        result.update(
            state="blocked",
            verdict="blocked",
            value=None,
            margin=None,
            message=str(error) or "Invalid input or rule configuration.",
        )
    return result
