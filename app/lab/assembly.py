"""Assemble a complete test case. No cloud dependency and no inferred measurements."""

import hashlib
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from .calculations import evaluate_rule
from .extraction import schemas
from .reviewed_calculations import transformer_rows, transformer_rows_for_fields

TITLES = {
    "customer_request": "Customer request and test scope",
    "work_instruction": "Work instruction and test conditions",
    "transformer_proforma": "Transformer identification and nameplate",
    "loss_measurement": "Loss measurements",
    "loss_calculation": "Corrected losses and impedance",
    "routine_test": "Routine tests and no-load current",
    "short_circuit": "Short-circuit test",
    "temperature_rise": "Temperature-rise test",
    "pressure_oil_leakage": "Oil leakage and pressure test",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def assemble(job):
    fields = []
    documents = list(job.documents.order_by("created_at"))
    docs = {str(d.pk): d for d in documents}
    for f in job.fields.select_related("document").order_by("key"):
        fields.append(
            {
                "id": f.pk,
                "key": f.key,
                "label": f.label,
                "value": f.value,
                "unit": f.unit,
                "status": f.status,
                "origin": f.origin,
                "page": f.page,
                "document_id": str(f.document_id) if f.document_id else None,
                "version": f.version,
                "extraction_status": f.context.get("extraction_status", ""),
                "schema_key": f.context.get(
                    "schema_key", re.split(r"\.p\d+\.", f.key, maxsplit=1)[-1]
                ),
                "schema_page": f.context.get("schema_page", f.page or 1),
                "form_type": f.document.form_type if f.document else f.context.get("form_type", ""),
                "source": (
                    f.document.original_name
                    if f.document
                    else f.context.get("source_reference", "Digital entry")
                ),
            }
        )
    schema_map = {s["form_type"]: s for s in schemas()}
    sections, findings, calculations = [], [], []

    def finding(code, severity, title, detail, action, evidence=()):
        findings.append(
            {
                "id": code,
                "severity": severity,
                "title": title,
                "detail": detail,
                "action": action,
                "evidence": list(evidence),
            }
        )

    request = job.request_snapshot or {}
    for name in (
        "customer",
        "customer_address",
        "manufacturer",
        "sample_particulars",
        "requested_tests",
    ):
        if name in request and getattr(job, name) != request[name]:
            finding(
                "request-mismatch-" + name,
                "blocker",
                "Customer request does not match report metadata",
                name.replace("_", " ") + " differs from the customer-submitted request.",
                "Keep the original request unchanged; resolve registration data and create a new revision.",
            )

    if not job.report_scope:
        finding(
            "scope",
            "blocker",
            "Confirm the requested tests",
            "The report scope has not been confirmed against the customer request.",
            "Select the applicable report sections and record why other tests are excluded.",
        )
    elif not job.scope_note.strip():
        finding(
            "scope-note",
            "blocker",
            "Document the test scope",
            "No scope justification is recorded.",
            "Record the requested tests and why other sections are excluded.",
        )
    test_sections = set(TITLES) - {"customer_request", "work_instruction", "transformer_proforma"}
    if job.report_scope and not test_sections.intersection(job.report_scope):
        finding(
            "no-test-scope",
            "blocker",
            "Select at least one test section",
            "Administrative records alone do not form a test report.",
            "Confirm which tests were requested.",
        )
    if job.report_scope and not any(
        f["form_type"] in test_sections.intersection(job.report_scope)
        and f["status"] == "verified"
        and f["value"].strip()
        for f in fields
    ):
        finding(
            "no-test-result",
            "blocker",
            "No verified test result in scope",
            "The selected tests contain no verified measurement or observation.",
            "Record and review the applicable test results.",
        )
    for name, value in [
        ("customer", job.customer),
        ("sample_code", job.sample_code),
        ("test_series", job.test_series),
    ]:
        if not value.strip():
            finding(
                "metadata-" + name,
                "blocker",
                "Missing " + name.replace("_", " "),
                "The report header cannot identify the complete test case.",
                "Complete the job details.",
            )
    included = list(
        dict.fromkeys(list(job.report_scope) + [f["form_type"] for f in fields if f["form_type"]])
    )
    for kind in TITLES:
        if kind not in included:
            continue
        rows = [f for f in fields if f["form_type"] == kind]
        present = {(f["schema_page"], f["schema_key"]) for f in rows}
        missing = [
            f"p{f['page']}.{f['key']}"
            for f in schema_map[kind]["fields"]
            if not f.get("optional") and (f["page"], f["key"]) not in present
        ]
        pending = sum(f["status"] not in ("verified", "not_applicable") for f in rows)
        sections.append(
            {
                "key": kind,
                "title": TITLES[kind],
                "fields": rows,
                "missing": missing,
                "pending": pending,
                "applicable": kind in job.report_scope,
            }
        )
        if kind in job.report_scope and missing:
            finding(
                "missing-" + kind,
                "blocker",
                TITLES[kind] + ": incomplete source data",
                f"{len(missing)} schema fields are not recorded. Missing entries are not interpreted as passed tests.",
                "Import the remaining entries or explicitly mark genuinely inapplicable fields during review.",
            )
        if pending:
            finding(
                "review-" + kind,
                "blocker",
                TITLES[kind] + ": review required",
                f"{pending} readings remain unreviewed or ambiguous.",
                "Compare the highlighted readings with their sources.",
                [f["id"] for f in rows if f["status"] not in ("verified", "not_applicable")],
            )
    other = [f for f in fields if not f["form_type"]]
    if other:
        sections.append(
            {
                "key": "other",
                "title": "Additional measurements and observations",
                "fields": other,
                "missing": [],
                "pending": 0,
                "applicable": True,
            }
        )
        if any(f["status"] not in ("verified", "not_applicable") for f in other):
            finding(
                "review-other",
                "blocker",
                "Additional readings need review",
                "Some digital readings remain unchecked.",
                "Review the additional readings.",
                [f["id"] for f in other if f["status"] not in ("verified", "not_applicable")],
            )
    for doc in documents:
        if doc.form_type and (
            doc.status != "ready" or not any(f["document_id"] == str(doc.pk) for f in fields)
        ):
            finding(
                "document-" + str(doc.pk),
                "blocker",
                "Source processing is incomplete",
                doc.original_name + ": " + doc.get_status_display(),
                "Retry extraction or import a reviewed transcription of this source.",
            )

    # Identity comparison uses reviewed evidence only. Normalisation removes punctuation, not digits.
    for canonical, aliases, header in [
        ("sample code", {"sample_code"}, job.sample_code),
        ("test series", {"test_series", "series"}, job.test_series),
        ("serial number", {"serial", "serial_number"}, ""),
    ]:
        evidence = [
            f
            for f in fields
            if f["schema_key"] in aliases and f["status"] == "verified" and f["value"].strip()
        ]
        values = {re.sub(r"[^A-Z0-9]", "", f["value"].upper()) for f in evidence}
        if header:
            values.add(re.sub(r"[^A-Z0-9]", "", header.upper()))
        if len(values) > 1:
            finding(
                "identity-" + canonical.replace(" ", "-"),
                "blocker",
                "Conflicting " + canonical,
                "Reviewed sources or the report header disagree: " + ", ".join(sorted(values)),
                "Check whether the documents belong to the same sample; resolve the discrepancy at its source.",
                [f["id"] for f in evidence],
            )
    for f in fields:
        if f["status"] == "verified" and not f["value"].strip():
            finding(
                "blank-" + str(f["id"]),
                "blocker",
                "Blank verified reading",
                f["label"],
                "Enter a value or mark Not applicable.",
                [f["id"]],
            )

    # Multiple copies of a form must not silently supply conflicting final readings.
    duplicates = defaultdict(list)
    for f in fields:
        if f["form_type"] and f["status"] == "verified":
            duplicates[(f["form_type"], f["schema_page"], f["schema_key"])].append(f)
    for (kind, page, key), group in duplicates.items():
        if len({(f["value"].strip(), f["unit"].strip()) for f in group}) > 1:
            finding(
                "duplicate-" + kind + "-" + str(page) + "-" + key,
                "blocker",
                "Conflicting copies of " + key,
                "Multiple reviewed sources assign different values or units to this report field.",
                "Resolve the source discrepancy; use separate jobs for distinct samples or test runs.",
                [f["id"] for f in group],
            )
    for key, unit, factors in [
        ("rated_power", "VA", {"VA": 1, "kVA": 1000}),
        ("rated_hv", "V", {"V": 1, "kV": 1000}),
        ("rated_lv", "V", {"V": 1, "kV": 1000}),
    ]:
        group = [f for f in fields if f["schema_key"] == key and f["status"] == "verified"]
        values = set()
        for f in group:
            try:
                if f["unit"] not in factors:
                    raise ValueError("Unit not confirmed")
                number = Decimal(f["value"]) * factors[f["unit"]]
                if not number.is_finite() or number <= 0:
                    raise ValueError("Invalid rating")
                values.add(number)
            except (ValueError, InvalidOperation):
                finding(
                    "rating-unit-" + str(f["id"]),
                    "blocker",
                    "Check rating and unit",
                    f["label"] + " cannot be compared as a positive numeric " + unit + " value.",
                    "Confirm the written number and explicit unit.",
                    [f["id"]],
                )
        if len(values) > 1:
            finding(
                "rating-" + key,
                "blocker",
                "Conflicting " + key.replace("_", " "),
                "Reviewed nameplate values differ after unit conversion.",
                "Check the customer request, proforma and test sheets belong to the same asset.",
                [f["id"] for f in group],
            )

    loss_sources = [
        (str(doc.pk), doc.pk.hex, doc.original_name, doc)
        for doc in documents
        if doc.form_type == "loss_calculation"
    ]
    digital_loss = list(
        job.fields.filter(document__isnull=True, context__form_type="loss_calculation")
    )
    if digital_loss:
        loss_sources.append((None, "digital", "Imported loss worksheet", None))
    for source_id, source_key, source_name, doc in loss_sources:
        try:
            rows = transformer_rows(doc) if doc else transformer_rows_for_fields(digital_loss)
            calculations.append(
                {
                    "document_id": source_id,
                    "form_type": "loss_calculation",
                    "source": source_name,
                    "rows": rows,
                    "profile": "Supplied 250 kVA, three-phase copper, 11 kV/433 V worksheet",
                    "error": "",
                }
            )
            for row in rows:
                for load in ("50", "100"):
                    if "total_loss_" + load not in row:
                        continue
                    actual, limit = row["total_loss_" + load], row["limit_" + load]
                    margin = limit - actual
                    pct = f" ({abs(margin)/limit*100:.2f}%)" if limit > 0 else ""
                    evidence = [
                        f["id"]
                        for f in fields
                        if f["document_id"] == source_id and f["form_type"] == "loss_calculation"
                    ]
                    finding(
                        "loss-" + source_key + "-" + row["stage"] + "-" + load,
                        "warning" if margin < 0 else "info",
                        f"{row['stage']}: total loss at {load}% load",
                        f"{actual:.2f} W against the recorded {limit:.2f} W limit; {abs(margin):.2f} W{pct} "
                        + ("above" if margin < 0 else "below")
                        + " the limit. This is a worksheet comparison, not overall certification.",
                        (
                            "If unexpected, check temperature, current scaling, units and the applicable guaranteed limit; do not alter the measurement to obtain a pass."
                            if margin < 0
                            else "Confirm the recorded guarantee and applicable standard before authorising the report."
                        ),
                        evidence,
                    )
        except (ValueError, KeyError, ZeroDivisionError) as error:
            calculations.append(
                {
                    "document_id": source_id,
                    "form_type": "loss_calculation",
                    "source": source_name,
                    "rows": [],
                    "error": str(error),
                }
            )
            finding(
                "calc-" + source_key,
                "blocker",
                "Loss calculation withheld",
                str(error),
                "Resolve the input or profile issue before approving the report.",
            )
    assigned_rules = list(job.rules.order_by("code", "version"))
    rule_context = {
        "requested_tests_text": job.requested_tests or request.get("requested_tests", ""),
        "requested_test_ids": job.report_test_ids,
    }
    rules = [
        evaluate_rule(r, fields, calculations, rule_context)
        for r in assigned_rules
        if r.code != "SC_OVERALL"
    ]
    rules += [
        evaluate_rule(r, fields, calculations, dict(rule_context, prior_results=rules))
        for r in assigned_rules
        if r.code == "SC_OVERALL"
    ]
    for rule in rules:
        if rule["verdict"] in ("blocked", "not_configured") or rule["rule_status"] != "confirmed":
            finding(
                "rule-" + rule["code"] + "-" + str(rule["version"]),
                "blocker",
                rule["title"],
                rule["message"],
                "Verify the input readings and confirm the rule with its source clause.",
            )
        elif rule["verdict"] == "fail":
            finding(
                "rule-" + rule["code"] + "-" + str(rule["version"]),
                "blocker",
                rule["title"],
                rule["message"],
                "Investigate the recorded result and source; do not alter the measurement to obtain a pass.",
                [item["id"] for item in rule["inputs"] if "id" in item],
            )
        elif rule["verdict"] == "marginal":
            finding(
                "rule-" + rule["code"] + "-" + str(rule["version"]),
                "warning",
                rule["title"],
                rule["message"],
                "Quality must inspect the original value, tolerance and test conditions.",
                [item["id"] for item in rule["inputs"] if "id" in item],
            )
    summary_keys = {
        "temperature_rise": {
            "top_oil_rise",
            "hv_winding_rise",
            "lv_winding_rise",
            "duration",
            "hv_observation",
            "lv_observation",
        },
        "pressure_oil_leakage": {
            "routine_pressure",
            "routine_pressure_duration",
            "routine_pressure_observation",
            "type_pressure",
            "type_pressure_duration",
            "type_observation",
            "vacuum",
            "vacuum_duration",
            "oil_duration",
            "oil_observation",
        },
        "short_circuit": {
            "condition_before",
            "required_normal_rms",
            "required_normal_peak",
            "required_high_rms",
            "required_high_peak",
            "required_low_rms",
            "required_low_peak",
        },
    }
    for kind, keys in summary_keys.items():
        observed = [
            f
            for f in fields
            if f["form_type"] == kind
            and f["schema_key"] in keys
            and f["status"] == "verified"
            and f["value"].strip()
        ]
        if observed:
            finding(
                "observations-" + kind,
                "info",
                TITLES[kind] + ": recorded evidence",
                "; ".join(f["label"] + ": " + f["value"] + " " + f["unit"] for f in observed),
                "Verify the test conditions, duration and applicable acceptance rule before interpreting these observations as a pass or failure.",
                [f["id"] for f in observed],
            )
    if not fields:
        finding(
            "no-data",
            "blocker",
            "No measurements recorded",
            "There is no test evidence to populate the report.",
            "Import measurements or enter them digitally.",
        )
    from .quality import assess, validation_policy

    quality = assess(list(job.fields.select_related("document")))
    arithmetic = defaultdict(list)
    for field_id, check in quality.items():
        for failure in check["failed"]:
            arithmetic[failure].append(field_id)
    for index, (message, evidence) in enumerate(arithmetic.items()):
        finding(
            "arithmetic-" + str(index),
            "warning",
            "Validation check needs review",
            message,
            "Compare the recorded values and the applicable check with the original source. Preserve a genuine discrepancy and explain it before approval.",
            evidence,
        )
    result = {
        "title": job.title,
        "customer": request.get("customer", job.customer),
        "sample_code": job.sample_code,
        "test_series": job.test_series,
        "file_number": job.file_number,
        "customer_address": request.get("customer_address", job.customer_address),
        "manufacturer": request.get("manufacturer", job.manufacturer),
        "sample_particulars": request.get("sample_particulars", job.sample_particulars),
        "requested_tests": request.get("requested_tests", job.requested_tests),
        "request_snapshot": request,
        "job_version": job.version,
        "scope": job.report_scope,
        "requested_test_ids": job.report_test_ids or None,
        "scope_note": job.scope_note,
        "fields": fields,
        "sections": sections,
        "findings": findings,
        "transformer_calculations": calculations,
        "calculations": rules,
        "verdict_summary": {
            name: sum(rule["verdict"] == name for rule in rules)
            for name in ("pass", "marginal", "fail", "blocked", "not_applicable", "not_configured")
        },
        "documents": [
            {"id": str(d.pk), "name": d.original_name, "sha256": d.sha256, "status": d.status}
            for d in documents
        ],
        "blockers": sum(f["severity"] == "blocker" for f in findings),
        "limitations": "Proposed report format. Recorded-limit comparisons do not establish overall conformity or CPRI certification.",
        "engine": "Evidence checks v1; transformer worksheet profile v1",
        "validation_profile": {
            "version": validation_policy()["version"],
            "sha256": digest(validation_policy()),
        },
    }
    from .models import AuditEvent

    latest_review = (
        AuditEvent.objects.filter(
            job=job,
            action__in=(
                "source_review_imported",
                "final_source_review_imported",
                "pending_37_source_review_imported",
                "source_review_provenance_appended",
            ),
        )
        .order_by("-created_at")
        .first()
    )
    result["reviewed_by_name"] = (
        (latest_review.details.get("reviewer_name") or latest_review.details.get("reviewer"))
        if latest_review
        else ""
    ) or "Not recorded"
    result["reviewed_at"] = latest_review.created_at.isoformat() if latest_review else None
    provenance = (
        AuditEvent.objects.filter(job=job, action="source_review_provenance_appended")
        .order_by("-created_at")
        .first()
    )
    result["source_review_provenance"] = (
        provenance.details.get("statement", "") if provenance else ""
    )
    from .template_mapping import apply_mapping

    apply_mapping(result, job.report_template)
    if (result.get("template_mapping") or {}).get(
        "name"
    ) == "CPRI-SCL-TR-v1" and not job.report_test_ids:
        finding(
            "fixed-individual-scope",
            "blocker",
            "Confirm individual requested tests",
            "The fixed certificate requires a test-by-test scope mapped to the customer request.",
            "Select the requested tests in report scope before issue.",
        )
    from .fixed_template import crf_exact_copy_mismatches

    for placeholder in crf_exact_copy_mismatches(result):
        finding(
            "crf-exact-" + placeholder,
            "blocker",
            "CRF exact-copy check failed",
            placeholder.replace("_", " ") + " differs from the customer-submitted request.",
            "Compare the request and source CRF, then correct the source transcription.",
        )
    mapped = {s["key"] for s in result["report_sections"]}
    for key in set(job.report_scope) - mapped:
        finding(
            "unmapped-" + key,
            "blocker",
            "Report template omits a requested section",
            TITLES.get(key, key),
            "Choose a template containing every applicable test section.",
        )
    result["blockers"] = sum(f["severity"] == "blocker" for f in findings)
    from .report_analysis import analysis

    result["report_analysis"] = analysis(result)
    result["evidence_hash"] = digest(result)
    return result
