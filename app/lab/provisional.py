"""In-memory, non-issuable engineering preview over unreviewed transcription.

The ordinary assembly and approval path continue to require verified evidence.
This module never updates fields or a persisted Report snapshot.
"""

import re
from copy import deepcopy
from types import SimpleNamespace

from .assembly import assemble
from .calculations import evaluate_rule
from .report_analysis import analysis
from .reviewed_calculations import transformer_rows_for_fields


def preview_for_job(job, context=None, promote_unreviewed=True):
    snapshot = deepcopy(assemble(job))
    for field in snapshot["fields"]:
        if field.get("form_type") == "temperature_rise" and field.get("schema_key", "").startswith(
            ("time_series.13.", "time_series.14.")
        ):
            field["preview_excluded"] = True
    candidate_fields = deepcopy(snapshot["fields"])
    promoted = set()
    normalizations = []
    for field in candidate_fields:
        if (
            promote_unreviewed
            and field.get("status") == "unreviewed"
            and not field.get("preview_excluded")
            and field.get("extraction_status") != "struck_out"
            and str(field.get("value") or "").strip()
        ):
            original, original_unit = field["value"], field.get("unit") or ""
            value = original.strip()
            # Strip a duplicate, explicitly visible unit; never infer a magnitude.
            suffix = original_unit.strip()
            if suffix and value.casefold().endswith(suffix.casefold()):
                numeric = value[: -len(suffix)].strip()
                if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", numeric):
                    field["value"] = numeric
            key = field.get("schema_key", "")
            if (
                (
                    field.get("form_type") == "temperature_rise"
                    and key in ("top_oil_rise", "hv_winding_rise", "lv_winding_rise")
                    or field.get("form_type") == "transformer_proforma"
                    and key in ("guaranteed_temp_rise_1", "guaranteed_temp_rise_2")
                )
                and field.get("unit", "") in ("", "°C", "degC", "C", "\ufffdC", "K")
                and re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", field["value"].strip())
            ):
                # A temperature *difference* has identical numerical value in °C and K.
                field["unit"] = "K"
            if field.get("form_type") == "loss_calculation":
                if key == "material" and value.casefold() == "cu wound":
                    field["value"] = "Cu"
                if key == "phases" and re.fullmatch(r"3\s*ph(?:ase)?", value, re.I):
                    field["value"] = "3"
                if key == "efficiency_level" and re.fullmatch(r"EEL\s*-?\s*1", value, re.I):
                    field["value"] = "1"
                if not field.get("unit") and re.fullmatch(
                    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", field["value"].strip()
                ):
                    # These units are printed in the supplied v2.17 worksheet
                    # column headings; the inference is restricted to preview.
                    inferred = None
                    if key.endswith(".temperature"):
                        inferred = "degC"
                    elif key.startswith("hv_resistance.") and re.search(r"\.R[123]$", key):
                        inferred = "Ohm"
                    elif key.startswith("lv_resistance.") and re.search(r"\.R[123]$", key):
                        inferred = "mOhm"
                    elif re.search(r"\.V[123]?$", key):
                        inferred = "V"
                    elif re.search(r"\.I[123]?$", key):
                        inferred = "A"
                    elif re.search(r"\.P[123]?$", key):
                        inferred = "W"
                    elif key.endswith(".f"):
                        inferred = "Hz"
                    if inferred:
                        field["unit"] = inferred
            if (field["value"], field.get("unit") or "") != (original, original_unit):
                normalizations.append(
                    {
                        "label": field.get("label") or key,
                        "original": original,
                        "original_unit": original_unit,
                        "preview": field["value"],
                        "preview_unit": field.get("unit") or "",
                    }
                )
            field["status"] = "verified"
            promoted.add(field["id"])

    calculations = []
    for doc in job.documents.filter(form_type="loss_calculation"):
        source_fields = [
            field for field in candidate_fields if field.get("document_id") == str(doc.pk)
        ]
        if not source_fields:
            continue
        candidates = [
            SimpleNamespace(
                key=field["key"],
                value=field["value"],
                unit=field.get("unit") or "",
                status=field["status"],
                context={"schema_key": field["schema_key"]},
            )
            for field in source_fields
        ]
        try:
            rows = transformer_rows_for_fields(candidates)
            calculations.append(
                {
                    "document_id": str(doc.pk),
                    "form_type": "loss_calculation",
                    "source": doc.original_name,
                    "rows": rows,
                    "error": "",
                }
            )
        except (ValueError, KeyError, ZeroDivisionError) as exc:
            calculations.append(
                {
                    "document_id": str(doc.pk),
                    "form_type": "loss_calculation",
                    "source": doc.original_name,
                    "rows": [],
                    "error": str(exc),
                }
            )

    assigned_rules = list(job.rules.order_by("code", "version"))
    rule_context = {
        "requested_tests_text": job.requested_tests
        or job.request_snapshot.get("requested_tests", ""),
        "requested_test_ids": job.report_test_ids,
    }
    rule_context.update(context or {})
    results = [
        evaluate_rule(rule, candidate_fields, calculations, rule_context)
        for rule in assigned_rules
        if rule.code != "SC_OVERALL"
    ]
    results += [
        evaluate_rule(
            rule, candidate_fields, calculations, dict(rule_context, prior_results=results)
        )
        for rule in assigned_rules
        if rule.code == "SC_OVERALL"
    ]
    for result in results:
        result["provisional"] = True
        result["unreviewed_input_count"] = sum(
            item.get("id") in promoted for item in result["inputs"]
        )
    snapshot["calculations"] = results
    snapshot["transformer_calculations"] = calculations
    snapshot["verdict_summary"] = {
        state: sum(result["verdict"] == state for result in results)
        for state in (
            "pass",
            "marginal",
            "fail",
            "blocked",
            "not_applicable",
            "not_configured",
            "descriptive",
        )
    }
    snapshot["provisional_preview"] = True
    snapshot["provisional_promoted_field_count"] = len(promoted)
    snapshot["provisional_normalizations"] = normalizations
    candidate_snapshot = dict(snapshot, fields=candidate_fields)
    snapshot["provisional_analysis"] = analysis(candidate_snapshot)
    snapshot["provisional_analysis_fields"] = candidate_fields
    return snapshot
