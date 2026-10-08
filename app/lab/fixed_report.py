"""Populate one frozen CPRI template model for both PDF and printable HTML."""

import fnmatch
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html import escape as html_escape
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    LongTable,
    PageBreakIfNotEmpty,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)

from .fixed_template import (
    CROSS_LOSS_ROWS,
    CROSS_STABILITY_ROWS,
    PLACEHOLDER,
    TEMPLATE_NAME,
    TESTS,
    crf_exact_copy_mismatches,
    validate_fixed_definition,
)
from .principal_tap import nominal_phase_ratio, principal_row_prefix

FONT = "VectorDejaVu"
if FONT not in pdfmetrics.getRegisteredFontNames():
    pdfmetrics.registerFont(TTFont(FONT, str(Path(__file__).with_name("fonts") / "DejaVuSans.ttf")))


def _number(value, unit="", decimals=None):
    try:
        number = Decimal(str(value))
        if number.is_finite():
            places = (
                decimals
                if decimals is not None
                else {
                    "W": 2,
                    "watt": 2,
                    "watts": 2,
                    "K": 1,
                    "°C": 1,
                    "%": 3,
                    "Ω": 4,
                    "mΩ": 4,
                }.get(unit)
            )
            if places is not None:
                quantum = Decimal("1").scaleb(-places)
                return format(number.quantize(quantum, rounding=ROUND_HALF_UP), f".{places}f")
            rendered = format(number, "f")
            return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    except (InvalidOperation, TypeError, ValueError):
        pass
    return str(value if value is not None else "")


def _clean_unit(unit):
    return {"GΉ": "GΩ", "GOhm": "GΩ", "Gohm": "GΩ", "Ohm": "Ω", "mOhm": "mΩ", "kpa": "kPa"}.get(
        str(unit or ""), str(unit or "")
    )


def _numeric_text(value, unit, decimals=None):
    """Strip only a repeated, explicit unit; the unit is rendered in its own column."""
    raw = str(value if value is not None else "").strip()
    unit = _clean_unit(unit)
    aliases = [unit]
    if unit == "GΩ":
        aliases += ["GΉ", "GOhm"]
    if unit == "Ω":
        aliases += ["Ohm"]
    if unit == "mΩ":
        aliases += ["mOhm"]
    for suffix in sorted((item for item in aliases if item), key=len, reverse=True):
        if raw.casefold().endswith(suffix.casefold()):
            candidate = raw[: -len(suffix)].strip()
            if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", candidate):
                raw = candidate
                break
    return _number(raw, unit, decimals)


def _mapped_field(data, form_type, spec, principal_prefix=None):
    key = spec["key"].replace("ratio.{principal}", principal_prefix or "ratio.-1")
    matches = [
        field
        for field in data.get("fields", [])
        if field.get("form_type") == form_type
        and field.get("schema_key") == key
        and field.get("extraction_status") != "struck_out"
        and not field.get("preview_excluded")
    ]
    if len(matches) != 1:
        return "[pending review]" if not matches else "[source conflict]"
    field = matches[0]
    if field.get("status") != "verified":
        return "[pending review]"
    if not str(field.get("value") or "").strip():
        return "Not recorded"
    return _numeric_text(field["value"], spec.get("unit", ""), spec.get("decimals"))


def _mapped_source(data, form_type, key):
    matches = [
        field
        for field in data.get("fields", [])
        if field.get("form_type") == form_type and field.get("schema_key") == key
    ]
    if len(matches) != 1:
        peers = [
            field
            for field in data.get("fields", [])
            if field.get("form_type") == form_type and field.get("document_id")
        ]
        if not peers:
            return "Source not located"
        field = peers[0]
        documents = {item.get("id"): item.get("name") for item in data.get("documents", [])}
        return f'{documents.get(field.get("document_id")) or field.get("source") or "Source document"}, p. 1 (field not transcribed)'
    field = matches[0]
    documents = {item.get("id"): item.get("name") for item in data.get("documents", [])}
    name = documents.get(field.get("document_id")) or field.get("source") or "Digital entry"
    if field.get("origin") == "digital" and not field.get("document_id"):
        return name
    return f'{name}, p. {field.get("page") or "?"}'


def _table_source(data, form_type, key):
    """Compact source citation; Annex A retains each full source filename."""
    source = _mapped_source(data, form_type, key)
    page = re.search(r",\s*p\.\s*([^\s,(]+)", source)
    aliases = {
        "routine_test": "Routine logsheet",
        "loss_measurement": "Loss logsheet",
        "loss_calculation": "Loss datasheet",
        "temperature_rise": "Temperature-rise logsheet",
        "short_circuit": "Short-circuit logsheet",
        "pressure_oil_leakage": "Pressure/oil logsheet",
    }
    if page and form_type in aliases:
        return f"{aliases[form_type]}, p. {page.group(1)}"
    return source


def mapped_review_rows(data, include_verified=False):
    """Only the explicit cells used by the frozen template, for reviewer handoff."""
    definition = (data.get("template_mapping") or {}).get("definition") or {}
    rows = []
    seen = set()

    def add(page_title, form_type, key, label):
        identity = (form_type, key)
        if identity in seen:
            return
        seen.add(identity)
        matches = [
            field
            for field in data.get("fields", [])
            if field.get("form_type") == form_type and field.get("schema_key") == key
        ]
        if not include_verified and len(matches) == 1 and matches[0].get("status") == "verified":
            return
        value = matches[0].get("value") if len(matches) == 1 else ""
        rows.append((page_title, label, str(value or ""), _mapped_source(data, form_type, key)))

    for page in definition.get("pages", []):
        for block in page.get("blocks", []):
            content = (
                str(block.get("value", ""))
                + " "
                + " ".join(str(cell) for line in block.get("rows", []) for cell in line)
            )
            for placeholder in PLACEHOLDER.findall(content):
                binding = definition.get("bindings", {}).get(placeholder, {})
                if binding.get("source") == "field":
                    alias = {"customer_name": "customer"}.get(placeholder, placeholder)
                    if data.get("source_checked_cover", {}).get(alias):
                        continue
                    add(
                        page["title"],
                        binding["form_type"],
                        binding["key"],
                        binding.get("label") or placeholder.replace("_", " ").title(),
                    )
                specs = binding.get("field_specs", [])
                prefix = None
                if any("{principal}" in spec["key"] for spec in specs):
                    try:
                        prefix = principal_row_prefix(
                            data.get("fields", []), "routine_test", "ratio", False
                        )
                    except ValueError:
                        prefix = None
                for spec in specs:
                    key = spec["key"].replace("ratio.{principal}", prefix or "ratio.-1")
                    add(
                        page["title"],
                        spec.get("form_type", binding.get("form_type")),
                        key,
                        spec["label"],
                    )
                if binding.get("source") == "chart" and binding.get("series_indices"):
                    for index in binding["series_indices"]:
                        for series in binding["series_keys"]:
                            add(
                                page["title"],
                                "temperature_rise",
                                f"time_series.{index}.{series}",
                                f'Heating curve point {index + 1}: {series.replace("_", " ")}',
                            )
    return rows


def _fields(data, binding):
    form = binding.get("form_type")
    patterns = binding.get("patterns") or [binding.get("key", "")]
    return [
        field
        for field in data.get("fields", [])
        if field.get("form_type") == form
        and any(fnmatch.fnmatchcase(field.get("schema_key", ""), pattern) for pattern in patterns)
        and field.get("extraction_status") != "struck_out"
        and not field.get("preview_excluded")
    ]


def _date_value(value):
    raw = str(value or "").strip()
    parts = re.split(r"\s+(?:and|&)\s+|\s*[,;]\s*", raw, flags=re.I)
    try:
        for part in parts:
            datetime.strptime(part.replace("/", "-").replace(".", "-"), "%d-%m-%Y")
        return raw if parts and all(parts) else "Source check required"
    except ValueError:
        return "Source check required"


def _field_value(data, name, binding, issued):
    cover_name = {"customer_name": "customer"}.get(name, name)
    if not issued and data.get("source_checked_cover", {}).get(cover_name):
        return str(data["source_checked_cover"][cover_name])
    matches = [field for field in _fields(data, binding) if str(field.get("value") or "").strip()]
    if len(matches) != 1:
        return "Not recorded" if not matches else "Source conflict — review required"
    field = matches[0]
    if field.get("status") != "verified":
        return "[pending review]"
    if name == "tests_requested":
        requested = (data.get("request_snapshot") or {}).get("requested_tests")
        if isinstance(requested, list) and field["value"] == str(requested):
            from .test_catalog import station_types

            labels = {item["form_type"]: item["label"] for item in station_types()}
            if requested and all(code in labels for code in requested):
                return "; ".join(labels[code] for code in requested)
    return (
        str(field["value"])
        if binding.get("exact_copy")
        else _numeric_text(
            field["value"], binding.get("unit") or field.get("unit"), binding.get("decimals")
        )
    )


def _scope_state(data, test_id):
    requested = data.get("requested_test_ids")
    if requested is None or test_id in requested:
        return True
    return "short_circuit" in requested and test_id in {
        "winding_resistance",
        "ratio_vector",
        "insulation_resistance",
        "separate_source",
        "induced",
        "load_loss_impedance",
        "total_loss",
    }


def _scope_label(data, test_id):
    requested = data.get("requested_test_ids")
    if requested is not None and test_id not in requested and _scope_state(data, test_id):
        return "Performed as part of short-circuit withstand"
    return ""


def _brief_basis(rule):
    if rule.get("version") == 2:
        return "Rule set v2 — pending CPRI adoption"
    source = rule.get("source_clause") or "Source clause not confirmed"
    if rule.get("version") == 2:
        for before, after in (
            ("IS 1180 (Part 1):2014 incl. Amendment 4", "IS 1180-1:2014+A4"),
            ("IS 2026 (Part 1):2011", "IS 2026-1:2011"),
            (
                "IS 2026 (Part 2):2010 method; limits from IS 1180 (Part 1)",
                "IS 2026-2 method / IS 1180-1 limit",
            ),
            ("IS 2026 (Part 3):2018", "IS 2026-3:2018"),
            ("IS 2026 (Part 5):2011", "IS 2026-5:2011"),
            ("; reviewed by team, pending CPRI adoption", ""),
        ):
            source = source.replace(before, after)
    return source


def _ratio_phase_display(rule):
    """Show the governing reviewed phase, not a phase-mean deviation."""
    if not rule.get("worst_phase") or rule.get("worst_phase_ratio") is None:
        return None
    deviation = Decimal(str(rule["worst_phase_deviation"]))
    return (
        f'{rule["worst_phase"]} {_number(rule["worst_phase_ratio"], "", 2)} ' f"({deviation:+.2f}%)"
    )


def _rule_rows(data, binding, issued):
    codes = set(binding.get("codes", []))
    rows = []
    for rule in data.get("calculations", []):
        if rule.get("code") not in codes:
            continue
        state = rule.get("verdict", "blocked")
        if rule.get("v2_status") == "DESCRIPTIVE" or rule.get("code") in data.get(
            "descriptive_rule_codes", []
        ):
            code = rule.get("code", "")
            if code.startswith("LOSS_") and code.endswith(("HTBT", "HTAT", "LTBT", "LTAT")):
                limit = "Principal-tap EEL limit only"
            elif code.startswith("LOSS_") and code.endswith("NTAT"):
                limit = "BT/AT comparison only"
            elif code.startswith(("IMPEDANCE_HT", "IMPEDANCE_LT")):
                limit = "No tap-specific declaration"
            else:
                limit = "No separate acceptance limit"
            result, margin = "DESCRIPTIVE", "—"
        elif rule.get("v2_status") == "N/A" or state == "not_applicable":
            result, limit, margin = "NOT APPLICABLE", "Not assigned", "—"
        elif rule.get("v2_status") == "OBSERVATION" and state == "pass":
            result, limit, margin = (
                (
                    "OBSERVATION: NO LEAKAGE"
                    if rule.get("code") in ("PRESSURE_TEST", "OIL_LEAKAGE")
                    else (
                        "OBSERVATION: NO ABNORMALITIES"
                        if rule.get("code") == "SC_OVERALL"
                        else "OBSERVATION: WITHSTOOD"
                    )
                ),
                "Observed condition",
                "—",
            )
        else:
            marginal_label = (
                "PASS (NEAR LIMIT) ⚠" if rule.get("version") == 2 else "PASS (MARGINAL) ⚠"
            )
            result = {
                "pass": "PASS ✓",
                "fail": "FAIL ✗",
                "marginal": marginal_label,
                "not_applicable": "NOT REQUESTED",
                "blocked": "Review required",
                "not_configured": "Not configured",
            }.get(state, "Review required")
            if not issued and state in ("pass", "fail", "marginal"):
                result = "PROVISIONAL " + result
            unit = rule.get("unit") or rule.get("margin_unit") or ""
            limit = (
                _number(rule.get("limit"), unit) + (" " + unit if unit else "")
                if rule.get("limit") is not None
                else "Not recorded"
            )
            if rule.get("display_basis"):
                limit = rule["display_basis"].split(" within ", 1)[-1]
            if (
                rule.get("parameters", {}).get("v2_kind") == "ratio"
                and rule.get("limit") is not None
            ):
                limit = "±" + _number(rule["limit"], "%", 2) + "% [CLAUSE TBC]"
            margin = (
                _number(rule.get("margin"), rule.get("margin_unit") or unit, 2)
                + (
                    " " + (rule.get("margin_unit") or unit)
                    if rule.get("margin_unit") or unit
                    else ""
                )
                if rule.get("margin") is not None
                else "Not recorded"
            )
        unit = rule.get("unit") or ""
        if rule.get("v2_status") == "OBSERVATION":
            measured = (
                (rule.get("value") or "No leakage at any point")
                if state == "pass"
                and rule.get("code") in ("PRESSURE_TEST", "OIL_LEAKAGE", "SC_OVERALL")
                else (
                    "Withstood (BT and AT)"
                    if state == "pass"
                    else (
                        "[pending review]"
                        if rule.get("unreviewed_input_count")
                        else "Observation requires review"
                    )
                )
            )
        else:
            kind = rule.get("parameters", {}).get("v2_kind")
            value_places = 3 if kind == "reactance" else 2 if kind == "ratio" else None
            measured = (_ratio_phase_display(rule) if kind == "ratio" else None) or (
                _number(rule.get("value"), unit, value_places) + (" " + unit if unit else "")
                if rule.get("value") is not None
                else "Not recorded"
            )
        rows.append([rule.get("title") or rule.get("code"), measured, limit, margin, result])
    return rows or [
        ["No assigned acceptance rule", "See readings", "Not assigned", "—", "DESCRIPTIVE"]
    ]


def _test_summary(data, test_id, title, codes, issued):
    if not _scope_state(data, test_id):
        return [title, "Not requested by customer", "—", "—", "—", "NOT REQUESTED"]
    rules = [
        rule
        for rule in data.get("calculations", [])
        if rule.get("code") in codes
        and rule.get("code") not in data.get("descriptive_rule_codes", [])
    ]
    active_rules = [
        rule
        for rule in rules
        if rule.get("v2_status") not in ("DESCRIPTIVE", "N/A")
        and rule.get("verdict") not in ("descriptive", "not_applicable")
    ]
    if not active_rules:
        return [
            title,
            _scope_label(data, test_id) or "No assigned acceptance rule",
            "Not assigned",
            "See test section",
            "—",
            "DESCRIPTIVE",
        ]
    rules = active_rules
    active = [
        rule
        for rule in rules
        if rule.get("verdict") not in ("blocked", "not_configured", "not_applicable")
    ]
    if any(rule.get("verdict") == "fail" for rule in rules):
        result = "FAIL ✗"
    elif any(rule.get("verdict") in ("blocked", "not_configured") for rule in rules):
        result = "Review required"
    elif any(rule.get("verdict") == "marginal" for rule in rules):
        result = (
            "PASS (NEAR LIMIT) ⚠"
            if all(rule.get("version") == 2 for rule in rules)
            else "PASS (MARGINAL) ⚠"
        )
    elif all(rule.get("v2_status") == "OBSERVATION" for rule in rules):
        result = (
            "OBSERVATION: NO LEAKAGE"
            if all(rule.get("code") in ("PRESSURE_TEST", "OIL_LEAKAGE") for rule in rules)
            else "OBSERVATION: WITHSTOOD"
        )
    else:
        result = "PASS ✓"
    if not issued and result.startswith(("PASS", "FAIL")):
        result = "PROVISIONAL " + result
    worst = min(
        (rule for rule in active if rule.get("margin") is not None),
        key=lambda rule: Decimal(str(rule["margin"])),
        default=active[0] if active else None,
    )
    if worst:
        unit = worst.get("unit") or ""
        kind = worst.get("parameters", {}).get("v2_kind")
        value_places = 3 if kind == "reactance" else 2 if kind == "ratio" else None
        measured = (
            (worst.get("value") or "No leakage at any point")
            if worst.get("v2_status") == "OBSERVATION"
            and worst.get("verdict") == "pass"
            and worst.get("code") in ("PRESSURE_TEST", "OIL_LEAKAGE")
            else (
                "Withstood (BT and AT)"
                if worst.get("v2_status") == "OBSERVATION" and worst.get("verdict") == "pass"
                else (
                    "[pending review]"
                    if worst.get("v2_status") == "OBSERVATION"
                    and worst.get("unreviewed_input_count")
                    else (_ratio_phase_display(worst) if kind == "ratio" else None)
                    or _number(worst.get("value"), unit, value_places)
                    + (" " + unit if unit else "")
                )
            )
        )
        limit = (
            _number(worst.get("limit"), unit) + (" " + unit if unit else "")
            if worst.get("limit") is not None
            else "Not recorded"
        )
        if worst.get("v2_status") == "OBSERVATION":
            limit = "Withstand without reported failure"
        if worst.get("display_basis"):
            limit = worst["display_basis"].split(" within ", 1)[-1]
        if worst.get("parameters", {}).get("v2_kind") == "ratio" and worst.get("limit") is not None:
            limit = "±" + _number(worst["limit"], "%", 2) + "% [CLAUSE TBC]"
        margin = (
            "—"
            if worst.get("v2_status") == "OBSERVATION"
            else (
                _number(worst.get("margin"), worst.get("margin_unit") or unit, 2)
                + (
                    " " + (worst.get("margin_unit") or unit)
                    if worst.get("margin_unit") or unit
                    else ""
                )
                if worst.get("margin") is not None
                else "Not recorded"
            )
        )
        clause = _brief_basis(worst)
        if _scope_label(data, test_id):
            clause = _scope_label(data, test_id) + "; " + clause
    else:
        measured, limit, margin, clause = (
            "Not recorded",
            "Not recorded",
            "—",
            "Source clause not confirmed",
        )
    return [title, clause, limit, measured, margin, result]


def _mapped_readings(data, binding):
    specs = binding.get("field_specs", [])
    test_id = binding.get("test_id")
    form_type = binding.get("form_type")
    if test_id == "winding_resistance":
        rows = []
        by_key = {spec["key"]: spec for spec in specs}
        for index in range(3):
            tap_key = f"hv_resistance.{index}.tap"
            tap = _mapped_field(data, form_type, by_key[tap_key])
            label = {"N": "HV normal tap", "H": "HV high tap", "L": "HV low tap"}.get(
                tap.upper(),
                f"HV tap position {index+1} {tap}" if tap.startswith("[") else "HV tap " + tap,
            )
            values = [
                _mapped_field(data, form_type, by_key[f"hv_resistance.{index}.{stage}_{phase}"])
                for stage in ("BT", "AT")
                for phase in (1, 2, 3)
            ]
            rows.append(
                [
                    label,
                    *values,
                    "Ω",
                    (
                        "Checked"
                        if all(
                            value not in ("[pending review]", "[source conflict]")
                            for value in values + [tap]
                        )
                        else "Pending review"
                    ),
                ]
            )
        values = [
            _mapped_field(data, form_type, by_key[f"lv_resistance.0.{stage}_{phase}"])
            for stage in ("BT", "AT")
            for phase in (1, 2, 3)
        ]
        rows.append(
            [
                "LV winding",
                *values,
                "mΩ",
                (
                    "Checked"
                    if all(
                        value not in ("[pending review]", "[source conflict]") for value in values
                    )
                    else "Pending review"
                ),
            ]
        )
        return rows
    if test_id == "ratio_vector":
        try:
            prefix = principal_row_prefix(data.get("fields", []), form_type, "ratio", False)
        except ValueError:
            prefix = None
        values = [
            _mapped_field(data, spec.get("form_type", form_type), spec, prefix) for spec in specs
        ]
        label = (
            "Principal tap " + values[0]
            if values[0] not in ("[pending review]", "[source conflict]")
            else "Principal tap [pending review]"
        )
        return [
            [
                label,
                *values[1:],
                "",
                (
                    "Checked"
                    if all(
                        value not in ("[pending review]", "[source conflict]") for value in values
                    )
                    else "Pending review"
                ),
            ]
        ]
    rows = []
    for spec in specs:
        key = spec["key"]
        source_form = spec.get("form_type", form_type)
        value = _mapped_field(data, source_form, spec)
        rows.append(
            [
                spec["label"],
                value,
                _clean_unit(spec["unit"]),
                (
                    "Checked"
                    if value not in ("[pending review]", "[source conflict]")
                    else "Pending review"
                ),
                _table_source(data, source_form, key),
            ]
        )
    return rows or [["No direct reading defined; see calculated results", "—", "", "—", "—"]]


def _verified_number(data, form_type, key):
    matches = [
        field
        for field in data.get("fields", [])
        if field.get("form_type") == form_type
        and field.get("schema_key") == key
        and field.get("status") == "verified"
        and not field.get("preview_excluded")
    ]
    if len(matches) != 1:
        return None
    try:
        return Decimal(str(_numeric_text(matches[0]["value"], matches[0].get("unit", ""))))
    except (InvalidOperation, ValueError):
        return None


def _phase_mean(data, form_type, prefix, stage, phases):
    values = [_verified_number(data, form_type, f"{prefix}.{stage}_{phase}") for phase in phases]
    if (
        any(value is None for value in values)
        or min(values) <= 0
        or max(values) / min(values) > Decimal("1.05")
    ):
        return None
    return sum(values) / len(values)


def _comparison(before, after, unit, precision, convention):
    if before is None or after is None:
        return "[pending review]", "[pending review]", "[pending review]"
    shown_before = _number(before, unit, precision)
    shown_after = _number(after, unit, precision)
    b, a = Decimal(shown_before), Decimal(shown_after)
    if convention == "stability":
        change = (b - a) / b * 100 if b else None
        result = f"{change:+.3f}" if change is not None else "Not recorded"
    elif convention == "descriptive":
        result = "Descriptive"
    elif unit == "W":
        result = f"{a-b:+.2f}"
    else:
        result = f"{(a-b)/b*100:+.2f}" if b else "Not recorded"
    return shown_before, shown_after, result


def _cross_test_rows(data, kind, row_specs=None):
    stages = {
        row.get("stage"): row
        for calculation in data.get("transformer_calculations", [])
        for row in calculation.get("rows", [])
    }
    rows = []
    for spec in (
        row_specs
        if row_specs is not None
        else (CROSS_STABILITY_ROWS if kind == "cross_test_stability" else CROSS_LOSS_ROWS)
    ):
        category, unit = spec["kind"], spec["unit"]
        if category in ("stability", "loss"):
            tap, metric = spec["stage"], spec["metric"]
            before, after = ((stages.get(tap + stage) or {}).get(metric) for stage in ("BT", "AT"))
        elif category in ("resistance", "ratio"):
            try:
                nominal = (
                    nominal_phase_ratio(data.get("fields", []))[0] if category == "ratio" else None
                )
                prefix = (
                    principal_row_prefix(
                        data.get("fields", []),
                        spec["form_type"],
                        spec["family"],
                        nominal_ratio=nominal,
                    )
                    if spec["tap"] == "principal"
                    else spec["family"] + "." + spec["tap"]
                )
                phases = "ABC" if category == "ratio" else (1, 2, 3)
                before = _phase_mean(data, spec["form_type"], prefix, "BT", phases)
                after = _phase_mean(data, spec["form_type"], prefix, "AT", phases)
            except ValueError:
                before = after = None
        else:
            before = _verified_number(data, spec["form_type"], spec["prefix"] + "_BT")
            after = _verified_number(data, spec["form_type"], spec["prefix"] + "_AT")
        b, a, change = _comparison(
            before,
            after,
            unit,
            spec["precision"],
            (
                "stability"
                if category == "stability"
                else "descriptive" if category == "ir" else "change"
            ),
        )
        rows.append([spec["label"], b, a, unit, change])
    return rows


def _system(data, key, report, issued, binding=None):
    if key == "report_no":
        from .verification_link import issued_report_number

        return (
            "Not allocated"
            if data.get("synthetic_demo") and not issued
            else issued_report_number(report)
        )
    if key == "file_no":
        return data.get("file_number") or "Not allocated"
    if key == "revision":
        return str(report.revision)
    if key == "issue_date":
        return report.approved_at.strftime("%d %B %Y") if issued else "Not issued"
    if key == "sample_code":
        return (
            data.get("source_checked_cover", {}).get("sample_code")
            if not issued and data.get("source_checked_cover", {}).get("sample_code")
            else data.get("sample_code") or "Not recorded"
        )
    if key == "test_series":
        return (
            data.get("source_checked_cover", {}).get("test_series")
            if not issued and data.get("source_checked_cover", {}).get("test_series")
            else data.get("test_series") or "Not recorded"
        )
    if key in ("receipt_date", "test_period"):
        return (
            data.get("source_checked_cover", {}).get(key)
            if not issued and data.get("source_checked_cover", {}).get(key)
            else data.get(key) or "Not recorded"
        )
    if key == "overall_result":
        if not issued:
            return (
                "Draft result only. Final conformity requires completed review and authorisation."
            )
        verdicts = data.get("calculations", [])
        failed = [
            item.get("title", item.get("code", "check"))
            for item in verdicts
            if item.get("verdict") == "fail"
        ]
        marginal = [
            item.get("title", item.get("code", "check"))
            for item in verdicts
            if item.get("verdict") == "marginal" and item.get("version") != 2
        ]
        if failed:
            return "Did not pass: " + ", ".join(failed) + "."
        if marginal:
            return "Passed; marginal: " + ", ".join(marginal) + "."
        return "Passed all tests listed."
    if key == "signatures":
        def authorisation(user, at):
            if not issued or not user or not at:
                return "Pending"
            name = user.get_full_name().strip() or str(user)
            return f"{name} — {timezone.localtime(at).strftime('%d %B %Y, %H:%M %Z')}"

        return [
            [
                "Tested by (Engineer)",
                authorisation(getattr(report, "engineer_locked_by", None), getattr(report, "engineer_locked_at", None)),
            ],
            [
                "Verified by (Quality)",
                authorisation(getattr(report, "quality_verified_by", None), getattr(report, "quality_verified_at", None)),
            ],
            ["Approved by (HoD)", authorisation(getattr(report, "approved_by", None), report.approved_at)],
        ]
    if key == "summary_rows":
        return [
            [str(index), *_test_summary(data, test_id, title, codes, issued)]
            for index, (test_id, title, _, _, codes) in enumerate(TESTS, 1)
        ]
    if key in ("cross_test", "cross_test_losses", "cross_test_stability"):
        mapped = (binding or {}).get("row_specs")
        if key == "cross_test_stability":
            return _cross_test_rows(data, key, mapped)
        if key == "cross_test_losses":
            return _cross_test_rows(data, key, mapped)
        return [
            [row[0], row[1], row[2], row[4]]
            for row in _cross_test_rows(data, "cross_test_losses")
            + _cross_test_rows(data, "cross_test_stability")
        ]
    if key == "observations":
        return [
            f"{rule.get('title')}: "
            + (
                f"within {_number(rule.get('margin'), rule.get('margin_unit'), 2)} {rule.get('margin_unit') or ''} of limit."
                if rule.get("version") == 2 and rule.get("verdict") == "marginal"
                else f"{rule.get('verdict', '').upper()} — measured {_number(rule.get('value'), rule.get('unit'))} {rule.get('unit') or ''}; margin {_number(rule.get('margin'), rule.get('margin_unit'), 2)} {rule.get('margin_unit') or ''}."
            )
            for rule in data.get("calculations", [])
            if rule.get("verdict") in ("marginal", "fail")
        ] or ["No marginal or failed rule result recorded."]
    if key == "source_records":
        rows = []
        for item in data.get("documents", []):
            linked = [
                field
                for field in data.get("fields", [])
                if field.get("document_id") == item.get("id")
            ]
            pages = sorted({field.get("page") for field in linked if field.get("page")})
            date_keys = (
                "date_BT",
                "date_AT",
                "date",
                "test_dates",
                "pressure_date",
                "type_date",
                "oil_date",
            )
            dates = []
            for field in sorted(
                linked,
                key=lambda item: (
                    date_keys.index(item.get("schema_key"))
                    if item.get("schema_key") in date_keys
                    else len(date_keys)
                ),
            ):
                key_name = field.get("schema_key")
                if (
                    field.get("status") != "verified"
                    or key_name not in date_keys
                    or not field.get("value")
                ):
                    continue
                label = {
                    "date_BT": "BT",
                    "date_AT": "AT",
                    "test_dates": "Test dates",
                    "date": "Test date",
                    "pressure_date": "Pressure date",
                    "oil_date": "Oil date",
                    "type_date": "Type dates",
                }[key_name]
                entry = f'{label}: {_date_value(field["value"])}'
                if entry not in dates:
                    dates.append(entry)
            pending_date = any(
                field.get("status") != "verified"
                and field.get("value")
                and field.get("schema_key") in date_keys
                for field in linked
            )
            rows.append(
                [
                    item.get("name") or "Not recorded",
                    ", ".join(str(page) for page in pages) or "Not recorded",
                    (
                        "; ".join(dates)
                        if dates
                        else "[pending review]" if pending_date else "Not recorded"
                    ),
                ]
            )
        if data.get("synthetic_demo") and not rows:
            entries = sorted(
                {
                    field.get("source", "")
                    for field in data.get("fields", [])
                    if field.get("status") == "verified"
                    and field.get("source", "").startswith("Station entry —")
                }
            )
            rows = [
                [entry.rsplit(", ", 1)[0], "—", entry.rsplit(", ", 1)[-1]]
                for entry in entries
            ]
        return rows or [["Not recorded", "—", "—"]]
    if key == "instruments":
        return [
            field["value"]
            for field in data.get("fields", [])
            if field.get("schema_key") == "instrument_serials"
            and field.get("status") == "verified"
            and field.get("value")
        ] or ["[pending review]"]
    if key == "review_summary":
        mapped = mapped_review_rows(data, include_verified=True)
        pending = len(mapped_review_rows(data))
        verified = len(mapped) - pending
        reviewer = data.get("reviewed_by_name") or "Not recorded"
        reviewed_at = data.get("reviewed_at") or "Not recorded"
        if reviewed_at != "Not recorded":
            try:
                reviewed_at = datetime.fromisoformat(str(reviewed_at)).strftime(
                    "%Y-%m-%d %H:%M:%S %z"
                )
            except (ValueError, TypeError):
                pass
        return [
            f"Template-bound checked readings: {verified}. Template-bound readings awaiting review: {pending}.",
            f"Source reviewer: {reviewer}. Review timestamp: {reviewed_at}.",
            data.get("source_review_provenance") or "Source review provenance: Not recorded.",
            "Source corrections retain actor, timestamp, old value and new value in the job audit log.",
        ]
    if key == "rule_provenance":
        groups = {}
        for item in data.get("calculations", []):
            basis = item.get("source_clause") or "Not recorded"
            if item.get("version") == 2:
                basis = basis.replace("; reviewed by team, pending CPRI adoption", "")
            if item.get("parameters", {}).get("v2_kind") == "temperature":
                basis = re.sub(r"\s*\([^)]*stricter than[^)]*\)", "", basis, flags=re.I)
                basis += "; IS limit: [TBC]"
            elif (
                item.get("limit_provenance")
                and item.get("parameters", {}).get("v2_kind") == "eel_principal"
            ):
                basis += "; governing limit: " + item["limit_provenance"]
            status = (
                "team-reviewed; CPRI pending"
                if item.get("version") == 2
                else item.get("rule_status") or "Not recorded"
            )
            groups.setdefault((basis, status), []).append(item.get("title") or "Engineering check")
        return [
            [", ".join(titles), basis, status] for (basis, status), titles in groups.items()
        ] or [["Not recorded", "—", "—"]]
    if key == "abbreviations":
        return [
            "BT — before test; AT — after test; HV — high voltage; LV — low voltage; IR — insulation resistance."
        ]
    return "Not recorded"


def _resolve(data, name, binding, report, issued):
    source = binding["source"]
    if source == "field":
        return _field_value(data, name, binding, issued)
    if source == "system":
        value = _system(data, binding["key"], report, issued, binding)
        return value
    test_id = binding.get("test_id")
    if test_id and not _scope_state(data, test_id):
        if source in ("conclusion", "remark"):
            return "Not requested by customer"
        width = 2 if name.startswith("conditions.") else 4 if source == "calculated" else 5
        return [["Not requested by customer"] + ["—"] * (width - 1)]
    if source in ("fields", "calculated", "rules", "conclusion", "remark"):
        if source == "fields":
            if "field_specs" in binding:
                if name.startswith("conditions."):
                    return [
                        [
                            "Scope",
                            _scope_label(data, test_id) or "As requested in the customer request",
                        ]
                    ]
                return _mapped_readings(data, binding)
            fields = _fields(data, binding)
            if name.startswith("conditions."):
                selected = [
                    field
                    for field in data.get("fields", [])
                    if field.get("form_type") == binding["form_type"]
                    and field.get("schema_key")
                    in (
                        "date",
                        "date_BT",
                        "date_AT",
                        "test_dates",
                        "ambient_BT",
                        "ambient_AT",
                        "ambient",
                        "method",
                        "standard",
                        "selected_tap",
                    )
                ]
                rows = (
                    [["Scope", _scope_label(data, test_id)]] if _scope_label(data, test_id) else []
                )
                rows += [
                    [
                        field.get("label") or field.get("schema_key"),
                        (
                            _date_value(field.get("value"))
                            if field.get("schema_key", "").startswith(("date", "test_dates"))
                            else field.get("value") or "Not recorded"
                        ),
                    ]
                    for field in selected
                ]
                return rows or [["Test conditions", "Not recorded"]]
            source_ids = {
                document.get("id"): index
                for index, document in enumerate(data.get("documents", []), 1)
            }
            rows = []
            for field in sorted(
                fields,
                key=lambda item: ("." in item.get("schema_key", ""), item.get("schema_key", "")),
            ):
                unit = field.get("unit") or ""
                value = field.get("value") or "Not recorded"
                if value != "Not recorded" and unit == "W":
                    value = _number(value, unit)
                review = "Checked" if field.get("status") == "verified" else "Unreviewed"
                if test_id == "ratio_vector" and re.fullmatch(
                    r"ratio\.\d+\.(BT|AT)_[ABC]", field.get("schema_key", "")
                ):
                    prefix = field["schema_key"].rsplit("_", 1)[0]
                    group = [
                        item
                        for item in fields
                        if item.get("schema_key", "").startswith(prefix + "_")
                    ]
                    try:
                        numbers = [Decimal(str(item["value"])) for item in group]
                        if (
                            len(numbers) == 3
                            and min(numbers) > 0
                            and max(numbers) / min(numbers) > Decimal("1.05")
                        ):
                            review = "Source check required"
                    except (InvalidOperation, ValueError, TypeError):
                        pass
                source = (
                    f"Source {source_ids[field['document_id']]}, p. {field.get('page') or '?'}"
                    if field.get("document_id") in source_ids
                    else field.get("source") or "Digital entry"
                )
                rows.append(
                    [field.get("label") or field.get("schema_key"), value, unit, review, source]
                )
            cap = (
                2
                if test_id == "short_circuit"
                else (
                    4
                    if test_id in ("load_loss_impedance", "total_loss")
                    else 6 if test_id == "temperature_rise" else 10
                )
            )
            if len(rows) > cap:
                overflow = len(rows) - cap
                rows = rows[:cap] + [
                    ["Additional source readings in evidence register", str(overflow), "", "—", "—"]
                ]
            return rows or [["Not recorded", "Not recorded", "", "Not recorded", "—"]]
        if source == "calculated":
            if "metrics" in binding:
                if not binding["metrics"]:
                    return [["No separate calculated value; see engineering verdict", "—", "", "—"]]
                rows = []
                for calculation in data.get("transformer_calculations", []):
                    for row in calculation.get("rows", []):
                        for metric in binding["metrics"]:
                            value = row.get(metric["key"])
                            if value is not None:
                                rows.append(
                                    [
                                        str(row.get("stage") or "Stage not recorded")
                                        + " · "
                                        + metric["label"],
                                        _number(value, metric["unit"], metric["decimals"]),
                                        metric["unit"],
                                        "Calculated by VectorLab (v2.17 method)",
                                    ]
                                )
                return rows or [["Not recorded", "Not recorded", "", "—"]]
            if test_id not in ("load_loss_impedance", "total_loss", "short_circuit"):
                return [["No calculation applied", "—", "", "—"]]
            rows = []
            for calculation in data.get("transformer_calculations", []):
                for row in calculation.get("rows", []):
                    stage = row.get("stage", "Not recorded")
                    metrics = (
                        [
                            ("Load loss at 75 °C", "load_loss_100", "W"),
                            ("Impedance at 75 °C", "Z75", "%"),
                        ]
                        if test_id == "load_loss_impedance"
                        else (
                            [
                                ("Total loss at 50%", "total_loss_50", "W"),
                                ("Total loss at 100%", "total_loss_100", "W"),
                            ]
                            if test_id == "total_loss"
                            else [("Reactance", "X50", "%")]
                        )
                    )
                    for title, key, unit in metrics:
                        if row.get(key) is not None:
                            rows.append(
                                [
                                    stage + " · " + title,
                                    _number(row[key], unit),
                                    unit,
                                    calculation.get("source") or "Worksheet",
                                ]
                            )
            cap = (
                3
                if test_id == "short_circuit"
                else 4 if test_id in ("load_loss_impedance", "total_loss") else 6
            )
            if len(rows) > cap:
                overflow = len(rows) - cap
                rows = rows[:cap] + [
                    ["Additional calculated rows in evidence register", str(overflow), "", "—"]
                ]
            return rows or [["Not recorded", "Not recorded", "", "—"]]
        if source == "rules":
            return _rule_rows(data, binding, issued)
        if source == "conclusion":
            rules = [
                rule
                for rule in data.get("calculations", [])
                if rule.get("code") in binding.get("codes", [])
            ]
            if not rules:
                return "Recorded readings are descriptive; no acceptance rule is assigned."
            if any(rule.get("verdict") == "fail" for rule in rules):
                return "The configured check indicates a failure; confirm the source and applicable rule before issue."
            if any(rule.get("verdict") in ("blocked", "not_configured") for rule in rules):
                return "The verdict requires source or rule review before issue."
            if any(rule.get("verdict") == "marginal" for rule in rules):
                if all(rule.get("version") == 2 for rule in rules):
                    return (
                        "The configured check meets its limit. The near-limit margin is recorded "
                        "for Quality review."
                        if not issued
                        else "The configured check meets its limit. The near-limit margin was reviewed by Quality."
                    )
                return "The configured check is marginal; Quality review is required."
            return (
                "The configured checks show a pass."
                if issued
                else "The configured checks show a pass; final approval is required before issue."
            )
        if source == "remark":
            return "No additional engineer remark recorded."
    if source == "chart":
        values = []
        for index in binding.get("series_indices", range(30)):
            series = []
            for key in binding.get(
                "series_keys", ("top_oil", "bottom_oil", "ambient_1", "ambient_2", "ambient_3")
            ):
                found = [
                    field
                    for field in data.get("fields", [])
                    if field.get("form_type") == "temperature_rise"
                    and field.get("schema_key") == f"time_series.{index}.{key}"
                    and not field.get("preview_excluded")
                    and ("series_indices" not in binding or field.get("status") == "verified")
                ]
                try:
                    series.append(float(found[0]["value"]) if len(found) == 1 else None)
                except (TypeError, ValueError):
                    series.append(None)
            if all(value is not None for value in series):
                elapsed = binding.get("elapsed_hours", [])
                hour = elapsed[binding["series_indices"].index(index)] if elapsed else index
                values.append([hour, series[0], series[1], sum(series[2:]) / 3])
        return values
    return "Not recorded"


def compile_fixed(report):
    data = report.snapshot
    definition = (data.get("template_mapping") or {}).get("definition") or {}
    validate_fixed_definition(definition)
    issued = bool(report.approved_at)
    if issued and crf_exact_copy_mismatches(data):
        raise ValueError("Issued report CRF fields do not exactly match the submitted request.")
    values = {
        key: _resolve(data, key, binding, report, issued)
        for key, binding in definition["bindings"].items()
    }

    def fill(text):
        return PLACEHOLDER.sub(lambda match: str(values.get(match.group(1), "Not recorded")), text)

    pages = []
    for page in definition["pages"]:
        blocks = []
        for block in page["blocks"]:
            item = {
                key: value for key, value in block.items() if key not in ("value", "text", "rows")
            }
            if "text" in block:
                item["text"] = fill(block["text"])
            if "rows" in block:
                resolved = []
                for row in block["rows"]:
                    cells = [fill(str(cell)) for cell in row]
                    if len(row) == 2:
                        match = PLACEHOLDER.fullmatch(str(row[1]))
                        if match and definition["bindings"][match.group(1)].get("label"):
                            cells[0] = definition["bindings"][match.group(1)]["label"]
                    resolved.append(cells)
                item["rows"] = resolved
            if "value" in block:
                match = PLACEHOLDER.fullmatch(block["value"])
                item["value"] = values[match.group(1)] if match else fill(block["value"])
            blocks.append(item)
        pages.append({"id": page["id"], "title": page["title"], "blocks": blocks})
    return {
        "template": TEMPLATE_NAME,
        "version": (data.get("template_mapping") or {}).get("version", 1),
        "sample_code": values["sample_code"],
        "report_no": values["report_no"],
        "revision": report.revision,
        "issued": issued,
        "synthetic_demo": bool(data.get("synthetic_demo")),
        "pages": pages,
    }


def _empty_chart_message(report):
    data = report.snapshot
    binding = (
        ((data.get("template_mapping") or {}).get("definition") or {})
        .get("bindings", {})
        .get("heating_curve", {})
    )
    keys = {
        f"time_series.{index}.{series}"
        for index in binding.get("series_indices", [])
        for series in binding.get("series_keys", [])
    }
    if any(
        field.get("form_type") == "temperature_rise"
        and field.get("schema_key") in keys
        and field.get("status") != "verified"
        for field in data.get("fields", [])
    ):
        return "Heating curve: [pending review]."
    return "Heating curve: Not recorded."


def render_fixed_html(report):
    model = compile_fixed(report)
    css = """@page{size:A4;margin:16mm}body{font:11px Arial,sans-serif;color:#111}section{break-before:page}section:first-of-type{break-before:auto}h1{font-size:18px}h2{font-size:13px;margin-top:18px}table{border-collapse:collapse;width:100%;margin:8px 0}th,td{border-bottom:1px solid #aaa;padding:5px;text-align:left;vertical-align:top}th{background:#eee}.draft{font-weight:bold;border:1px solid #555;padding:8px}.footer{font-size:9px;margin-top:20px}"""
    out = [
        '<!doctype html><html><head><meta charset="utf-8"><title>CPRI report</title><style>',
        css,
        "</style></head><body>",
    ]
    if not model["issued"]:
        out.append('<p class="draft">AUTOMATED DRAFT — NOT ISSUED</p>')
    for page in model["pages"]:
        out += ["<section><h1>", html_escape(page["title"]), "</h1>"]
        if model["synthetic_demo"]:
            out.append('<p class="draft">SYNTHETIC DEMO — not a CPRI certificate</p>')
        for block in page["blocks"]:
            if block.get("title"):
                out += ["<h2>", html_escape(block["title"]), "</h2>"]
            kind = block["type"]
            rows = block.get("rows") or block.get("value")
            if kind in (
                "header_fields",
                "key_value_table",
                "data_table",
                "verdict_table",
                "signature_block",
            ):
                if isinstance(rows, str):
                    rows = [[rows]]
                if not rows:
                    rows = [["Not recorded"]]
                out.append("<table>")
                if block.get("columns"):
                    out += (
                        ["<thead><tr>"]
                        + [f"<th>{html_escape(str(col))}</th>" for col in block["columns"]]
                        + ["</tr></thead>"]
                    )
                for row in rows:
                    out += (
                        ["<tr>"]
                        + [f"<td>{html_escape(str(cell))}</td>" for cell in row]
                        + ["</tr>"]
                    )
                out.append("</table>")
            elif kind == "chart":
                if not rows:
                    out += ["<p>", html_escape(_empty_chart_message(report)), "</p>"]
                    continue
                out.append("<p>Heating curve data (hour, top oil, bottom oil, mean ambient):</p>")
                out.append(
                    "<table><tr><th>Hour</th><th>Top oil °C</th><th>Bottom oil °C</th><th>Mean ambient °C</th></tr>"
                )
                for row in rows or []:
                    out += (
                        ["<tr>"]
                        + [
                            f"<td>{html_escape(str(round(cell,1) if isinstance(cell,float) else cell))}</td>"
                            for cell in row
                        ]
                        + ["</tr>"]
                    )
                out.append("</table>")
            elif kind == "annexure_list":
                out += (
                    ["<ul>"]
                    + [
                        f"<li>{html_escape(str(value))}</li>"
                        for value in (rows if isinstance(rows, list) else [rows])
                    ]
                    + ["</ul>"]
                )
            else:
                out += ["<p>", html_escape(str(block.get("text", "Not recorded"))), "</p>"]
        out += [
            '<p class="footer">Sample ',
            html_escape(str(model["sample_code"])),
            " · Template ",
            TEMPLATE_NAME,
            " v",
            str(model["version"]),
            " · Revision ",
            str(model["revision"]),
            " · SYNTHETIC DEMO — not a CPRI certificate" if model["synthetic_demo"] else "",
            "</p></section>",
        ]
    out.append("</body></html>")
    return "".join(out)


def render_fixed_pdf(report):
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.charts.linecharts import HorizontalLineChart
    from reportlab.graphics.charts.textlabels import Label
    from reportlab.graphics.shapes import Drawing

    from .pdf_export import NumberedCanvas
    from .report_workflow import issue_code

    model = compile_fixed(report)
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            "FixedTitle",
            parent=styles["Heading1"],
            fontName=FONT,
            fontSize=13,
            leading=17,
            spaceAfter=9,
        )
    )
    styles.add(
        ParagraphStyle(
            "FixedHead",
            parent=styles["Heading2"],
            fontName=FONT,
            fontSize=9,
            leading=11,
            spaceBefore=5,
            spaceAfter=3,
        )
    )
    styles.add(
        ParagraphStyle(
            "FixedBody", parent=styles["BodyText"], fontName=FONT, fontSize=7.2, leading=9
        )
    )
    styles.add(
        ParagraphStyle(
            "FixedCell",
            parent=styles["FixedBody"],
            fontSize=6.0 if model["synthetic_demo"] else 6.4,
            leading=7.0 if model["synthetic_demo"] else 7.8,
        )
    )

    def paragraph(value, style="FixedBody"):
        return Paragraph(
            html_escape(str(value if value is not None else "Not recorded")).replace("\n", "<br/>"),
            styles[style],
        )

    def table(rows, columns=None):
        values = []
        if columns:
            values.append([paragraph(cell, "FixedCell") for cell in columns])
        for row in rows or [["Not recorded"]]:
            values.append([paragraph(cell, "FixedCell") for cell in row])
        width = 505 / max(len(row) for row in values)
        for row in values:
            row.extend([paragraph("")] * (int(505 / width) - len(row)))
        item = LongTable(values, colWidths=[width] * len(values[0]), repeatRows=1 if columns else 0)
        item.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#aaaaaa")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                ]
                if columns
                else [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#aaaaaa")),
                ]
            )
        )
        return item

    story = []
    for index, page in enumerate(model["pages"]):
        if index:
            story.append(PageBreakIfNotEmpty())
        story.append(paragraph(page["title"], "FixedTitle"))
        if not model["issued"] and index == 0:
            story.append(paragraph("AUTOMATED DRAFT — NOT ISSUED"))
        for block_index, block in enumerate(page["blocks"]):
            if block.get("title"):
                story.append(paragraph(block["title"], "FixedHead"))
            kind = block["type"]
            rows = block.get("rows") or block.get("value")
            if kind in (
                "header_fields",
                "key_value_table",
                "data_table",
                "verdict_table",
                "signature_block",
            ):
                if isinstance(rows, str):
                    rows = [[rows]]
                story.append(table(rows, block.get("columns")))
                if kind == "signature_block" and model["issued"]:
                    code = issue_code(report)
                    from .verification_link import report_verification_url

                    url = report_verification_url(report, code)
                    widget = QrCodeWidget(url)
                    x0, y0, x1, y1 = widget.getBounds()
                    drawing = Drawing(
                        55, 55, transform=[55 / (x1 - x0), 0, 0, 55 / (y1 - y0), -x0, -y0]
                    )
                    drawing.add(widget)
                    story += [
                        Spacer(1, 5),
                        drawing,
                        paragraph("Scan to verify this issued report."),
                    ]
            elif kind == "chart":
                if len(rows or []) >= 2:
                    chart = HorizontalLineChart()
                    chart.x = 65
                    chart.y = 30
                    chart.width = 380
                    chart.height = 90
                    chart.data = [[row[column] for row in rows] for column in (1, 2, 3)]
                    chart.categoryAxis.categoryNames = [str(row[0]) for row in rows]
                    chart.valueAxis.valueMin = 0
                    chart.lines[0].strokeColor = colors.black
                    chart.lines[1].strokeColor = colors.HexColor("#555555")
                    chart.lines[2].strokeColor = colors.HexColor("#999999")
                    chart.lines[1].strokeDashArray = [5, 3]
                    chart.lines[2].strokeDashArray = [1, 3]
                    drawing = Drawing(505, 145)
                    drawing.add(chart)
                    x_label = Label()
                    x_label.setOrigin(255, 8)
                    x_label.setText("Elapsed time (h)")
                    x_label.fontName = FONT
                    x_label.fontSize = 7
                    y_label = Label()
                    y_label.setOrigin(12, 75)
                    y_label.setText("Temperature (°C)")
                    y_label.angle = 90
                    y_label.fontName = FONT
                    y_label.fontSize = 7
                    drawing.add(x_label)
                    drawing.add(y_label)
                    story += [
                        paragraph("Top oil — solid; bottom oil — dashed; mean ambient — dotted."),
                        drawing,
                    ]
                else:
                    story.append(paragraph(_empty_chart_message(report)))
            elif kind == "annexure_list":
                for value in (rows if isinstance(rows, list) else [rows]):
                    story.append(paragraph("• " + str(value)))
            else:
                story.append(paragraph(block.get("text", "Not recorded")))
            if block_index < len(page["blocks"]) - 1:
                story.append(Spacer(1, 2))
    stream = BytesIO()
    doc = SimpleDocTemplate(
        stream,
        pagesize=(595, 842),
        leftMargin=45,
        rightMargin=45,
        topMargin=55,
        bottomMargin=55,
        title=f'{TEMPLATE_NAME} {model["sample_code"]}',
        author="VectorLab",
    )
    doc.build(
        story,
        canvasmaker=lambda *args, **kwargs: NumberedCanvas(
            *args,
            certificate=model["report_no"],
            sample=model["sample_code"],
            revision=model["revision"],
            provisional=not model["issued"],
            synthetic_demo=model["synthetic_demo"],
            mapping_version=model["version"],
            **kwargs,
        ),
    )
    return stream.getvalue()
