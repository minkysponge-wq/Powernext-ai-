"""Explainable triage. Passing a check is never recorded as human verification."""

import json
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path


def identity(field):
    return (
        field.document.form_type if field.document else field.context.get("form_type", ""),
        field.page or field.context.get("schema_page", 1),
        field.context.get("schema_key", re.split(r"\.p\d+\.", field.key, maxsplit=1)[-1]),
    )


def number(value):
    try:
        if len(value.strip()) > 64:
            return None
        result = Decimal(value.strip())
        return result if result.is_finite() and abs(result.as_tuple().exponent) <= 12 else None
    except (InvalidOperation, AttributeError):
        return None


def canonical_unit(value):
    """Compare written unit spellings without changing stored source text."""
    raw = str(value or "").strip().replace(" ", "").casefold()
    return {
        "°c": "c",
        "degc": "c",
        "degreesc": "c",
        "seconds": "s",
        "second": "s",
        "sec": "s",
        "watts": "w",
        "watt": "w",
        "ohms": "ohm",
    }.get(raw, raw)


def sensitive_separator_change(field):
    """Detect punctuation changes from the preserved first scan transcription."""
    if field.origin != "scan" or field.status != "unreviewed":
        return False
    key = identity(field)[2].lower()
    if not any(
        part in key for part in ("date", "serial", "sample_code", "series", "reference", "standard")
    ):
        return False
    if not field.raw_value.strip() or not field.value.strip():
        return False
    separators = lambda value: "".join(char for char in value if char in "./-_")
    return separators(field.raw_value) != separators(field.value)


@lru_cache(maxsize=1)
def validation_policy():
    """Versioned, trusted configuration; no stored Python is evaluated."""
    policy = json.loads(
        Path(__file__).with_name("validation_policy.json").read_text(encoding="utf-8")
    )
    validate_policy(policy)
    return policy


def validate_policy(policy):
    """Reject unsupported operations and unsafe or incomplete rule definitions."""
    if (
        not isinstance(policy, dict)
        or type(policy.get("version")) is not int
        or policy["version"] < 1
    ):
        raise ValueError("Validation profile needs a positive integer version.")
    for name in (
        "critical_key_fragments",
        "critical_review_fragments",
        "cross_document",
        "numeric_with_units",
    ):
        if not isinstance(policy.get(name), list) or any(
            not isinstance(x, str) or not x for x in policy[name]
        ):
            raise ValueError(f"Invalid {name} list.")
    if not isinstance(policy.get("aliases"), dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in policy["aliases"].items()
    ):
        raise ValueError("Invalid field aliases.")
    rules = policy.get("checks")
    if not isinstance(rules, list):
        raise ValueError("Validation checks must be a list.")
    allowed = {
        "required",
        "format",
        "range",
        "unit",
        "mean",
        "sum",
        "difference",
        "ratio",
        "monotonic",
        "limit",
    }
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") not in allowed:
            raise ValueError("Unsupported validation check type.")
        if any(
            key in rule and (not isinstance(rule[key], str) or not rule[key])
            for key in ("field", "form_type", "table", "unit")
        ):
            raise ValueError("Invalid check selector or unit.")
        kind = rule["type"]
        if kind in ("mean", "sum", "difference"):
            terms = rule.get("of")
            if (
                not isinstance(terms, list)
                or len(terms) < 2
                or any(not isinstance(x, str) or not x for x in terms)
                or len(set(terms)) != len(terms)
                or not isinstance(rule.get("equals"), str)
            ):
                raise ValueError("Arithmetic check needs distinct operands and a result.")
            if kind == "difference" and len(terms) != 2:
                raise ValueError("Difference needs two operands.")
        elif kind == "ratio":
            if (
                not isinstance(rule.get("of"), list)
                or len(rule["of"]) != 2
                or not all(isinstance(x, str) and x for x in rule["of"])
            ):
                raise ValueError("Ratio needs numerator and denominator fields.")
            if (
                not isinstance(rule.get("units"), list)
                or len(rule["units"]) != 2
                or not all(isinstance(x, str) and x for x in rule["units"])
            ):
                raise ValueError("Ratio needs explicit operand units.")
            for key in ("expect", "tol"):
                if number(str(rule.get(key, ""))) is None:
                    raise ValueError("Ratio expectation and tolerance must be finite numbers.")
            if number(str(rule["tol"])) < 0:
                raise ValueError("Ratio tolerance cannot be negative.")
        elif kind == "monotonic":
            if (
                not isinstance(rule.get("column"), str)
                or rule.get("direction") not in ("increasing", "decreasing")
                or not rule.get("unit")
            ):
                raise ValueError("Trend check needs a column, direction and unit.")
        elif kind == "unit":
            units = rule.get("allowed")
            if (
                not isinstance(rule.get("field"), str)
                or not rule["field"]
                or not isinstance(units, list)
                or not 1 <= len(units) <= 20
                or any(
                    not isinstance(unit, str) or not unit.strip() or len(unit) > 30
                    for unit in units
                )
            ):
                raise ValueError("Unit check needs a field selector and 1–20 allowed units.")
        else:
            if not isinstance(rule.get("field"), str) or not rule["field"]:
                raise ValueError("Field check needs a field selector.")
            if kind == "format" and rule.get("format") not in (
                "decimal",
                "integer",
                "identifier",
                "date_iso",
                "boolean_presence",
                "choice_label",
            ):
                raise ValueError("Unsupported field format.")
            if kind in ("range", "limit"):
                if not any(k in rule for k in ("min", "max")):
                    raise ValueError("Range or limit needs a bound.")
                for key in ("min", "max", "near_margin"):
                    if key in rule and number(str(rule[key])) is None:
                        raise ValueError("Range or limit bound must be finite.")
                if (
                    "min" in rule
                    and "max" in rule
                    and number(str(rule["min"])) > number(str(rule["max"]))
                ):
                    raise ValueError("Range or limit minimum exceeds maximum.")
                if "near_margin" in rule and number(str(rule["near_margin"])) < 0:
                    raise ValueError("Near-boundary margin cannot be negative.")
                if kind == "limit" and not rule.get("source_clause"):
                    raise ValueError("A limit requires a confirmed source clause.")
        if (
            "tol" in rule
            and kind in ("mean", "sum", "difference")
            and number(str(rule["tol"])) is None
        ):
            raise ValueError("Arithmetic tolerance must be finite.")
        if "tol" in rule and kind in ("mean", "sum", "difference") and number(str(rule["tol"])) < 0:
            raise ValueError("Arithmetic tolerance cannot be negative.")
    return policy


def _format_ok(value, kind):
    text = value.strip()
    if kind == "decimal":
        return number(text) is not None
    if kind == "integer":
        return bool(re.fullmatch(r"[+-]?\d+", text))
    if kind == "identifier":
        return len(text) <= 100 and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._/-]*", text))
    if kind == "boolean_presence":
        return text.casefold() in (
            "yes",
            "no",
            "true",
            "false",
            "present",
            "absent",
            "signed",
            "unsigned",
        ) or text in ("✓", "✔", "☑", "✗", "×", "☐")
    if kind == "choice_label":
        return len(text) <= 200 and text not in ("✓", "✔", "☑", "✗", "×", "☐", "-", "—")
    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        return False


def requires_individual_review(field, policy=None):
    """Scanned measurements need source review; digital entry has station ownership."""
    policy = validation_policy() if policy is None else policy
    key = identity(field)[2].lower()
    base_critical = any(fragment in key for fragment in policy["critical_key_fragments"])
    scan_critical = getattr(field, "origin", "scan") == "scan" and (
        any(fragment in key for fragment in policy["critical_review_fragments"])
        or (number(field.value) is not None and bool(field.unit.strip()))
    )
    return base_critical or scan_critical


def _extra_checks(fields, tables, checks, policy):
    """Plausibility checks flag problems but never confer automatic approval."""
    for rule in policy["checks"]:
        kind = rule["type"]
        if kind in ("mean", "sum", "difference"):
            continue
        if kind in ("required", "format", "range", "limit", "unit"):
            for f in fields:
                form, _, key = identity(f)
                if (
                    rule.get("form_type") not in (None, form)
                    or not fnmatchcase(key, rule["field"])
                    or f.status == "not_applicable"
                ):
                    continue
                c = checks[f.pk]
                if kind == "required":
                    if not f.value.strip():
                        c["issues"].append("Required field is blank.")
                    else:
                        c["passed"].append("Required field is present (not verified).")
                elif kind == "format":
                    if f.value.strip() and not _format_ok(f.value, rule["format"]):
                        msg = f"Format check failed: expected {rule['format']}."
                        c["issues"].append(msg)
                        c["failed"].append(msg)
                    elif f.value.strip():
                        c["passed"].append("Format check passed (not verified).")
                elif kind == "unit":
                    if not f.value.strip():
                        continue
                    allowed_units = {canonical_unit(unit) for unit in rule["allowed"]}
                    actual_unit = canonical_unit(f.unit)
                    if actual_unit not in allowed_units:
                        msg = "Unit is missing or outside the configured list; confirm against source."
                        c["issues"].append(msg)
                        c["failed"].append(msg)
                    else:
                        c["passed"].append("Unit spelling is allowed (reading not verified).")
                else:
                    if not f.value.strip():
                        continue
                    value = number(f.value)
                    label = "Configured limit" if kind == "limit" else "Plausibility range"
                    if value is None or (
                        "unit" in rule and canonical_unit(f.unit) != canonical_unit(rule["unit"])
                    ):
                        c["issues"].append(
                            f"{label} needs a numeric value and the configured unit."
                        )
                        continue
                    lower = number(str(rule["min"])) if "min" in rule else None
                    upper = number(str(rule["max"])) if "max" in rule else None
                    outside = (lower is not None and value < lower) or (
                        upper is not None and value > upper
                    )
                    if outside:
                        msg = (
                            f"{label} exceeded: recorded {value} {f.unit}; expected "
                            + (
                                f"{lower} to {upper}"
                                if lower is not None and upper is not None
                                else (
                                    f"at least {lower}" if lower is not None else f"at most {upper}"
                                )
                            )
                            + "."
                        )
                        c["issues"].append(msg)
                        c["failed"].append(msg)
                    else:
                        c["passed"].append(f"{label} passed (not verified).")
                        margin = number(str(rule.get("near_margin", "0")))
                        if margin and (
                            (lower is not None and value - lower <= margin)
                            or (upper is not None and upper - value <= margin)
                        ):
                            c["issues"].append(
                                f"{label} is close to a boundary; review individually."
                            )
        elif kind == "ratio":
            numerator, denominator = rule["of"]
            for (_, form, _, table, _), columns in tables.items():
                if rule.get("form_type") not in (None, form) or not fnmatchcase(
                    table, rule.get("table", "*")
                ):
                    continue
                if numerator not in columns or denominator not in columns:
                    continue
                members = [columns[numerator], columns[denominator]]
                values = [number(f.value) for f in members]
                if (
                    any(v is None for v in values)
                    or values[1] == 0
                    or [canonical_unit(f.unit) for f in members]
                    != [canonical_unit(unit) for unit in rule["units"]]
                ):
                    for f in members:
                        checks[f.pk]["issues"].append(
                            "Ratio check needs nonzero numeric inputs in configured units."
                        )
                    continue
                actual = values[0] / values[1]
                target = number(str(rule["expect"]))
                tol = number(str(rule["tol"]))
                passed = abs(actual - target) <= tol
                msg = f"Ratio check: calculated {actual}, expected {target} ± {tol}."
                for f in members:
                    checks[f.pk]["passed" if passed else "issues"].append(msg)
                    if not passed:
                        checks[f.pk]["failed"].append(msg)
        elif kind == "monotonic":
            series = defaultdict(list)
            for (source, form, page, table, row), columns in tables.items():
                if rule.get("form_type") not in (None, form) or not fnmatchcase(
                    table, rule.get("table", "*")
                ):
                    continue
                if rule["column"] in columns:
                    series[(source, form, page, table)].append((int(row), columns[rule["column"]]))
            for rows in series.values():
                if len(rows) < 2:
                    continue
                members = [f for _, f in sorted(rows)]
                values = [number(f.value) for f in members]
                if any(v is None for v in values) or any(
                    canonical_unit(f.unit) != canonical_unit(rule["unit"]) for f in members
                ):
                    for f in members:
                        checks[f.pk]["issues"].append(
                            "Trend check needs numeric values in configured units."
                        )
                    continue
                good = all(
                    (a <= b if rule["direction"] == "increasing" else a >= b)
                    for a, b in zip(values, values[1:])
                )
                msg = f"Trend check: values should be {rule['direction']} by row."
                for f in members:
                    checks[f.pk]["passed" if good else "issues"].append(msg)
                    if not good:
                        checks[f.pk]["failed"].append(msg)


def assess(fields, policy=None):
    policy = validation_policy() if policy is None else validate_policy(policy)
    checks = {
        f.pk: {"passed": [], "issues": [], "failed": [], "state": "needs_review"} for f in fields
    }
    aliases = policy["aliases"]
    repeat = defaultdict(list)
    tables = defaultdict(dict)
    corroborated = set()
    for f in fields:
        kind, page, key = identity(f)
        c = checks[f.pk]
        if not f.value.strip():
            c["issues"].append("No reading recorded; confirm whether applicable.")
        if f.status == "ambiguous":
            c["issues"].append("Previously marked ambiguous.")
        if requires_individual_review(f, policy):
            c["issues"].append("Critical source value: individual review required.")
        if (
            f.context.get("extraction_status") in ("missing", "illegible", "blank")
            and f.status == "unreviewed"
        ):
            c["issues"].append("Extraction did not provide a reliable reading.")
        if sensitive_separator_change(f):
            msg = "Date or identifier separators changed from the first scan reading; compare with the source."
            c["issues"].append(msg)
            c["failed"].append(msg)
        canonical = aliases.get(key, key)
        if canonical in policy["cross_document"]:
            repeat[canonical].append(f)
        if canonical in policy["numeric_with_units"] and (
            number(f.value) is None or not f.unit.strip()
        ):
            c["issues"].append("Confirm the numeric rating and explicit unit.")
        match = re.fullmatch(r"(.+)\.(\d+)\.([^.]+)", key)
        if match:
            group, row, col = match.groups()
            tables[
                (
                    (
                        str(f.document_id)
                        if f.document_id
                        else f.context.get("source_reference", "digital")
                    ),
                    kind,
                    page,
                    group,
                    row,
                )
            ][col] = f
    # Cross-source consistency does not infer equivalence of units.
    for key, group in repeat.items():
        sources = {
            str(f.document_id) if f.document_id else f.context.get("source_reference", "digital")
            for f in group
        }
        if len(sources) < 2:
            continue
        values = {
            (
                str(number(f.value)) if number(f.value) is not None else f.value.strip().casefold(),
                canonical_unit(f.unit),
            )
            for f in group
        }
        for f in group:
            passed = len(values) == 1 and bool(f.value.strip())
            checks[f.pk]["passed" if passed else "issues"].append(
                "Agrees across separate source records (not independent proof)."
                if len(values) == 1
                else "Sources disagree in value or unit."
            )
            if passed:
                corroborated.add(f.pk)
    for group, columns in tables.items():
        relations = []
        for rule in policy["checks"]:
            if rule["type"] not in ("mean", "sum", "difference"):
                continue
            if rule.get("form_type") not in (None, group[1]) or not fnmatchcase(
                group[3], rule.get("table", "*")
            ):
                continue
            terms, target = tuple(rule["of"]), rule["equals"]
            if all(x in columns for x in (*terms, target)):
                relations.append((terms, target, rule))
        for terms, target, rule in relations:
            operation = rule["type"]
            members = [columns[k] for k in (*terms, target)]
            vals = [number(f.value) for f in members]
            if all(f.status == "not_applicable" for f in members):
                continue
            if any(v is None for v in vals) or len({canonical_unit(f.unit) for f in members}) != 1:
                for f in members:
                    checks[f.pk]["issues"].append(
                        "Arithmetic check needs numeric values in matching units."
                    )
                continue
            if not members[0].unit.strip():
                for f in members:
                    checks[f.pk]["issues"].append(
                        "Arithmetic uses written numbers; confirm the missing unit before approval."
                    )
            expected = (
                sum(vals[:-1]) / len(terms)
                if operation == "mean"
                else (
                    sum(vals[:-1])
                    if operation == "sum"
                    else (
                        abs(vals[1] - vals[0]) if rule.get("absolute", False) else vals[1] - vals[0]
                    )
                )
            )
            # Half a last-place unit in each written value, propagated through the operation.
            precision = [Decimal("0.5") * Decimal(10) ** v.as_tuple().exponent for v in vals]
            tolerance = (
                sum(precision[:-1]) / len(terms) if operation == "mean" else sum(precision[:-1])
            ) + precision[-1]
            if "tol" in rule:
                tolerance = number(str(rule["tol"]))
            passed = abs(expected - vals[-1]) <= tolerance
            message = f"{operation.title()} check: calculated {expected}, recorded {vals[-1]}, rounding allowance {tolerance}."
            for f in members:
                checks[f.pk]["passed" if passed else "issues"].append(message)
                if not passed:
                    checks[f.pk]["failed"].append(message)
                else:
                    corroborated.add(f.pk)
    _extra_checks(fields, tables, checks, policy)
    for f in fields:
        c = checks[f.pk]
        if f.status in ("verified", "not_applicable"):
            c["state"] = "engineer_reviewed"
        elif f.pk in corroborated and not c["issues"]:
            c["state"] = "automatically_checked"
        elif not c["issues"]:
            c["issues"].append(
                "No corroborating automatic check available; compare with the source."
            )
    return checks


def review_priority(field, check, policy=None):
    """Order a human's first pass; never change verification or acceptance."""
    if check["state"] != "needs_review":
        return False
    policy = validation_policy() if policy is None else policy
    key = identity(field)[2].lower()
    return bool(
        field.context.get("extraction_issue")
        or field.context.get("extraction_status") in ("missing", "illegible")
        or check["failed"]
        or any(fragment in key for fragment in policy["critical_key_fragments"])
        or any(
            "Sources disagree" in issue or "close to a boundary" in issue
            for issue in check["issues"]
        )
    )
