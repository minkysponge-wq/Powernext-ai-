"""Fictional, source-labelled inputs for the public fixed-template walkthrough.

These values are never copied from a customer job. The fixture is created only
for a new synthetic job; it does not alter review states on existing jobs.
"""

import json
import re
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .extraction import schemas
from .fixed_template import TESTS, report_field_specs
from .models import Field, Rule

WORKSHEET = json.loads(
    (Path(__file__).with_name("fixtures") / "synthetic_demo_v2.json").read_text(
        encoding="utf-8"
    )
)["values"]
STATION_POLICIES = json.loads(
    Path(__file__).with_name("station_types.json").read_text(encoding="utf-8")
)


def is_synthetic_walkthrough(job):
    """A local-only, exact fixture identity for the candidate-rule demo gate."""
    return (
        settings.DEBUG
        and job.sample_code == "SYN-FULL-001"
        and job.customer == "SYNTHETIC WALKTHROUGH — Example Power Systems"
        and job.test_series == "SYN-SERIES-001"
        and job.customer_user_id is not None
        and job.customer_user.username == "walkthrough_customer"
    )


def worksheet_unit(key):
    if key == "rated_power":
        return "kVA"
    if key in ("rated_hv", "rated_lv") or re.search(r"\.V[123]?$", key):
        return "V"
    if key.startswith("guarantee_") or re.search(r"\.P[123]?$", key):
        return "W"
    if key.startswith("hv_resistance.") and re.search(r"\.R[123]$", key):
        return "Ohm"
    if key.startswith("lv_resistance.") and re.search(r"\.R[123]$", key):
        return "mOhm"
    if key.endswith(".temperature"):
        return "degC"
    if re.search(r"\.I[123]?$", key):
        return "A"
    if key.endswith(".f"):
        return "Hz"
    return ""


def mapped_keys(definition):
    result = {
        (binding["form_type"], binding["key"])
        for binding in definition["bindings"].values()
        if binding.get("source") == "field"
    }
    for test_id, _, form_type, _, _ in TESTS:
        for spec in report_field_specs(test_id):
            result.add(
                (
                    spec.get("form_type", form_type),
                    spec["key"].replace("{principal}", "3"),
                )
            )
    chart = definition["bindings"]["heating_curve"]
    result.update(
        ("temperature_rise", f"time_series.{index}.{series}")
        for index in chart["series_indices"]
        for series in chart["series_keys"]
    )
    return result


def reading(job, form_type, key):
    request = job.request_snapshot
    exact = {
        "customer_name": str(request["customer"]),
        "customer": str(request["customer"]),
        "customer_address": str(request["customer_address"]),
        "manufacturer": str(request["manufacturer"]),
        "sample_description": str(request["sample_particulars"]),
        "requested_test": str(request["requested_tests"]),
        "sample_code": job.sample_code,
        "series": job.test_series,
        "test_series": job.test_series,
        "rated_power": "250",
        "rated_hv": "11000",
        "rated_lv": "433",
        "phases": "3",
        "frequency": "50",
        "vector_group": "Dyn11",
        "declared_ratio_principal": "44.00",
        "cooling": "ONAN",
        "serial_number": "SYN-1098",
        "serial": "SYN-1098",
        "no_load_100_voltage": "433",
        "drawing_numbers": "SYN-DWG-001",
        "witness_name": "Synthetic witness",
        "sample_suitable": "Suitable for test",
        "standard": "Synthetic demo specification; CPRI adoption pending",
        "assigned_to": "Demo station engineer",
        "guaranteed_total_loss_50": "980",
        "guaranteed_total_loss_100": "2930",
        "impedance_at_75": "4.5",
        "guaranteed_temp_rise_1": "35",
        "guaranteed_temp_rise_2": "40",
        "top_oil_rise": "26.1",
        "hv_winding_rise": "39.6",
        "lv_winding_rise": "34.3",
        "no_load_100_percent": "0.51",
        "no_load_112_percent": "1.28",
        "no_load_100_limit_percent": "2.00",
        "no_load_112_limit_percent": "2.00",
        "routine_pressure": "80",
        "routine_pressure_observation": "No leakage at any point",
        "type_pressure": "80",
        "type_pressure_duration": "30",
        "pressure_deflection_result": "1.08",
        "vacuum": "500",
        "vacuum_duration": "30",
        "vacuum_deflection_result": "2.87",
        "oil_observation": "No leakage at any point",
        "condition_before": "No abnormalities",
        "during_test": "No abnormalities",
        "after_test": "No abnormalities",
        "conductor_core_clamps": "No visible damage",
        "spacers": "Intact",
        "oil": "Clear",
        "date": "07-10-2026",
        "date_BT": "07-10-2026",
        "date_AT": "07-10-2026",
        "test_dates": "07-10-2026",
        "pressure_date": "07-10-2026",
        "type_date": "07-10-2026",
        "oil_date": "07-10-2026",
        "instrument_serials": "SYN-INSTR-001",
        "circular": "Ticked",
        "tap_range": "+5% to -10%",
        "tap_step": "2.5%",
        "no_visible_defects_confirmed": "confirmed",
        "overall_engineer_verdict": "confirmed",
    }
    if form_type == "loss_calculation" and key in ("sample_code", "series"):
        return exact[key], ""
    if form_type == "loss_calculation" and key in ("guarantee_50", "guarantee_100"):
        return ("980" if key.endswith("50") else "2930"), "W"
    if form_type == "loss_calculation" and key in WORKSHEET:
        value = WORKSHEET[key]
        return str(value), worksheet_unit(key)
    if key in exact:
        unit = {
            "rated_power": "kVA",
            "rated_hv": "V",
            "rated_lv": "V",
            "guaranteed_total_loss_50": "W",
            "guaranteed_total_loss_100": "W",
            "impedance_at_75": "%",
            "impedance_high_tap_at_75": "%",
            "impedance_low_tap_at_75": "%",
            "guaranteed_temp_rise_1": "K",
            "guaranteed_temp_rise_2": "K",
            "top_oil_rise": "K",
            "hv_winding_rise": "K",
            "lv_winding_rise": "K",
            "no_load_100_percent": "%",
            "no_load_100_voltage": "V",
            "no_load_112_percent": "%",
            "no_load_100_limit_percent": "%",
            "no_load_112_limit_percent": "%",
            "routine_pressure": "kPa",
        }.get(key, "")
        return exact[key], unit
    if key.startswith("ratio."):
        _, position, reading_key = key.split(".")
        index = int(position)
        if reading_key == "tap":
            return ("N" if index == 3 else "H" if index == 0 else "L" if index == 6 else str(index + 1)), ""
        nominal = (46.20, 45.10, 44.55, 44.00, 42.90, 41.80, 39.60)[index]
        phase = {"A": 0.00, "B": -0.02, "C": 0.03}[reading_key[-1]]
        after = 0.08 if reading_key.startswith("AT") else 0.0
        return f"{nominal + phase + after:.2f}", ""
    if key.startswith("hv_resistance."):
        _, position, reading_key = key.split(".")
        index = int(position)
        if key.endswith(".tap"):
            return {0: "N", 1: "H", 2: "L"}.get(index, "?"), ""
        if reading_key.startswith(("BT_", "AT_")):
            stage = {0: 0, 1: 2, 2: 4}[index] + int(reading_key.startswith("AT_"))
            phase = reading_key[-1]
            return WORKSHEET[f"hv_resistance.{stage}.R{phase}"], "Ω"
        return "", ""
    if key.startswith("lv_resistance."):
        stage = int(".AT_" in key)
        phase = key[-1]
        return WORKSHEET[f"lv_resistance.{stage}.R{phase}"], "mΩ"
    if key.startswith("ir_"):
        ir = {
            "ir_hv_earth_BT": "1.20", "ir_hv_earth_AT": "1.19",
            "ir_lv_earth_BT": "1.26", "ir_lv_earth_AT": "1.24",
            "ir_hv_lv_BT": "1.31", "ir_hv_lv_AT": "1.29",
        }
        return ir[key], "GΩ"
    if key.endswith("_observation_BT") or key.endswith("_observation_AT"):
        return "No disruptive discharge, withstood", ""
    if key.startswith("hv_voltage_"):
        return "28", "kV"
    if key.startswith("lv_voltage_"):
        return "3", "kV"
    if key.startswith("induced_voltage_"):
        return "866", "V"
    if key.startswith("induced_frequency_"):
        return "100", "Hz"
    if key.endswith("_duration_BT") or key.endswith("_duration_AT"):
        return "60", "s"
    if key.startswith("time_series."):
        index = int(key.split(".")[1])
        return str(25 + index * (2 if "oil" in key else 0.1)), "degC"
    if key.startswith("shots."):
        return "12.3", "kA" if "peak" in key else "kA"
    if "observation" in key or "condition" in key:
        return "No abnormalities", ""
    return "Synthetic recorded value", ""


def create_fields(job, actors, definition):
    """Create one explicitly synthetic record per schema cell on a new job."""
    needed = mapped_keys(definition)
    needed.update({("transformer_proforma", "tap_range"), ("transformer_proforma", "tap_step")})
    needed.update(
        ("routine_test", f"ratio.{index}.{suffix}")
        for index in range(7)
        for suffix in ("tap", "BT_A", "BT_B", "BT_C", "AT_A", "AT_B", "AT_C")
    )
    needed.add(("transformer_proforma", "circular"))
    needed.update(
        {
            ("loss_measurement", "no_load_100_limit_percent"),
            ("loss_measurement", "no_load_112_limit_percent"),
        }
    )
    for policy in STATION_POLICIES:
        needed.update((policy["form_type"], key) for key in policy["lock_required"])
        needed.add((policy["form_type"], policy["result_any_of"][0]))
    objects = []
    entered_at = timezone.localtime().strftime("%d %b %y %H:%M %Z")
    station_names = {
        "routine_test": "Routine",
        "short_circuit": "SC",
        "loss_measurement": "Loss",
        "loss_calculation": "Loss calculation",
        "transformer_proforma": "Proforma",
        "temperature_rise": "Temperature rise",
        "pressure_oil_leakage": "Pressure/oil",
    }
    for schema in schemas():
        form_type = schema["form_type"]
        actor = actors.get(form_type, actors["routine_test"])
        station = station_names.get(form_type, form_type.replace("_", " ").title())
        for spec in schema["fields"]:
            key = spec["key"]
            active = (form_type, key) in needed or (
                form_type == "loss_calculation" and key in WORKSHEET
            )
            value, unit = reading(job, form_type, key) if active else ("", "")
            objects.append(
                Field(
                    job=job,
                    key=f"synthetic.{form_type}.p{spec['page']}.{key}",
                    label=spec["label"],
                    value=value,
                    unit=unit,
                    raw_value=value,
                    origin="digital",
                    page=spec["page"],
                    status="verified" if active else "not_applicable",
                    context={
                        "form_type": form_type,
                        "schema_key": key,
                        "schema_page": spec["page"],
                        "source_reference": (
                            f"Station entry — {station}, {actor.get_full_name() or actor.username}, "
                            f"{entered_at}"
                        ),
                    },
                    updated_by=actor,
                )
            )
    Field.objects.bulk_create(objects)
    return len(objects)


def attach_demo_rules(job):
    """Assign the unmodified candidate v2 rules to the local synthetic job."""
    rules = list(Rule.objects.filter(version=2).order_by("code"))
    if len(rules) != 28 or any(rule.status != "assumed" for rule in rules):
        raise ValueError("Seed all 28 unchanged candidate v2 rules before the walkthrough.")
    job.rules.add(*rules)
    return len(rules)
