"""Versioned declarative report mapping. Never executes a stored template as code."""

import fnmatch

from django.core.exceptions import ValidationError

from .extraction import schemas


def validate_definition(value):
    if isinstance(value, dict) and value.get("format") == "CPRI-SCL-TR-v1":
        from .fixed_template import validate_fixed_definition

        return validate_fixed_definition(value)
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("sections"), list)
        or not value["sections"]
    ):
        raise ValidationError("Provide a sections array with at least one section.")
    if "report_title" in value and (
        not isinstance(value["report_title"], str)
        or not value["report_title"].strip()
        or len(value["report_title"]) > 160
    ):
        raise ValidationError("Report title must be 1–160 characters of text.")
    known = {s["form_type"]: {f["key"] for f in s["fields"]} for s in schemas()}
    seen = set()
    for s in value["sections"]:
        if (
            not isinstance(s, dict)
            or s.get("form_type") not in known
            or not isinstance(s.get("title"), str)
            or not s["title"].strip()
        ):
            raise ValidationError("Every section needs a known form_type and title.")
        if s["form_type"] in seen:
            raise ValidationError("Each form type may appear only once.")
        seen.add(s["form_type"])
        if not isinstance(s.get("fields"), list) or not s["fields"]:
            raise ValidationError("Every section needs field mappings.")
        used = set()
        for mapping in s["fields"]:
            if not isinstance(mapping, dict) or not isinstance(mapping.get("key"), str):
                raise ValidationError("Every mapping needs a key.")
            matches = {k for k in known[s["form_type"]] if fnmatch.fnmatchcase(k, mapping["key"])}
            if not matches or used & matches:
                raise ValidationError("Unknown or overlapping field mapping.")
            if "label" in mapping and not isinstance(mapping["label"], str):
                raise ValidationError("Mapping labels must be text.")
            used |= matches


def default_definition():
    from .assembly import TITLES

    selected = {
        "loss_calculation": {
            "rated_power",
            "rated_hv",
            "rated_lv",
            "phases",
            "material",
            "ambient",
            "frequency",
        },
        "temperature_rise": {
            "series",
            "sample_code",
            "customer",
            "rated_power",
            "rated_hv",
            "rated_lv",
            "standard",
            "method",
            "duration",
            "top_oil_rise",
            "hv_winding_rise",
            "lv_winding_rise",
            "hv_observation",
            "lv_observation",
        },
    }
    sections = []
    for schema in schemas():
        kind = schema["form_type"]
        keys = list(
            dict.fromkeys(
                f["key"]
                for f in schema["fields"]
                if "." not in f["key"]
                and not any(x in f["key"] for x in ("signature", "instrument_serial", "raw_"))
            )
        )
        if kind in selected:
            keys = [k for k in keys if k in selected[kind]]
        if kind == "pressure_oil_leakage":
            keys += ["deflection.*"]
        if kind == "short_circuit":
            keys += ["shots.*.tap", "shots.*.peak", "shots.*.rms_avg", "shots.*.duration"]
        if kind == "loss_measurement":
            keys += ["no_load.*.stage", "no_load.*.Vavg", "no_load.*.Iavg", "no_load.*.Wtotal"]
        sections.append(
            {"form_type": kind, "title": TITLES[kind], "fields": [{"key": k} for k in keys]}
        )
    return {"report_title": "Transformer test report", "sections": sections}


def apply_mapping(data, template):
    definition = template.definition if template else default_definition()
    validate_definition(definition)
    if definition.get("format") == "CPRI-SCL-TR-v1":
        data["report_sections"] = data["sections"]
        data["template_mapping"] = {
            "name": template.name,
            "version": template.version,
            "definition": definition,
            "id": template.pk,
        }
        data["report_title"] = definition.get("report_title") or "Transformer test report"
        data["supporting_field_count"] = 0
        return data
    sections = []
    displayed = set()
    for section in definition["sections"]:
        source = next((s for s in data["sections"] if s["key"] == section["form_type"]), None)
        if source is None:
            continue
        rows = []
        for mapping in section["fields"]:
            for f in source["fields"]:
                if fnmatch.fnmatchcase(f["schema_key"], mapping["key"]):
                    row = dict(f)
                    if mapping.get("label"):
                        row["label"] = mapping["label"]
                    rows.append(row)
                    displayed.add(f["id"])
        sections.append({**source, "title": section["title"], "fields": rows})
    data["report_sections"] = sections
    data["template_mapping"] = {
        "name": template.name if template else "Laboratory summary",
        "version": template.version if template else 2,
        "definition": definition,
        "id": template.pk if template else None,
    }
    data["report_title"] = definition.get("report_title") or "Laboratory test report"
    data["supporting_field_count"] = len(data["fields"]) - len(displayed)
    return data
