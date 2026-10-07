"""Versioned, declarative CPRI report layout and its binding linter."""

import fnmatch
import re
from copy import deepcopy

from django.core.exceptions import ValidationError

from .extraction import schemas

TEMPLATE_NAME = "CPRI-SCL-TR-v1"
PLACEHOLDER = re.compile(r"{{\s*([a-z][a-z0-9_.]*)\s*}}")
BLOCK_TYPES = {
    "header_fields",
    "key_value_table",
    "data_table",
    "verdict_table",
    "narrative",
    "chart",
    "signature_block",
    "annexure_list",
}
SYSTEM_KEYS = {
    "report_no",
    "file_no",
    "revision",
    "issue_date",
    "sample_code",
    "test_series",
    "receipt_date",
    "test_period",
    "overall_result",
    "signatures",
    "summary_rows",
    "cross_test",
    "cross_test_losses",
    "cross_test_stability",
    "observations",
    "source_records",
    "instruments",
    "review_summary",
    "rule_provenance",
    "abbreviations",
}

TESTS = [
    (
        "winding_resistance",
        "Winding resistance",
        "loss_measurement",
        ["hv_resistance.*", "lv_resistance.*"],
        [],
    ),
    (
        "ratio_vector",
        "Voltage ratio & vector group",
        "routine_test",
        ["ratio.*", "vector_group_*"],
        ["VOLTAGE_RATIO"],
    ),
    ("insulation_resistance", "Insulation resistance", "routine_test", ["ir_*"], []),
    (
        "separate_source",
        "Separate-source AC withstand",
        "routine_test",
        [
            "hv_voltage_*",
            "hv_duration_*",
            "hv_observation_*",
            "lv_voltage_*",
            "lv_duration_*",
            "lv_observation_*",
        ],
        ["SEPARATE_SOURCE_AC"],
    ),
    (
        "induced",
        "Induced overvoltage withstand",
        "routine_test",
        ["induced_*"],
        ["INDUCED_DIELECTRIC"],
    ),
    (
        "no_load",
        "No-load loss & current",
        "loss_measurement",
        ["no_load_*", "no_load.*"],
        ["NO_LOAD_CURRENT_100", "NO_LOAD_CURRENT_112"],
    ),
    (
        "load_loss_impedance",
        "Load loss & impedance at 75 °C",
        "loss_calculation",
        ["load_measurement.*", "hv_resistance.*", "lv_resistance.*"],
        [
            "IMPEDANCE_NTBT",
            "IMPEDANCE_NTAT",
            "IMPEDANCE_HTBT",
            "IMPEDANCE_HTAT",
            "IMPEDANCE_LTBT",
            "IMPEDANCE_LTAT",
        ],
    ),
    (
        "total_loss",
        "Total loss 50% / 100% vs EEL",
        "loss_calculation",
        ["guarantee_50", "guarantee_100", "no_load.*"],
        [
            "LOSS_50_NTBT",
            "LOSS_50_NTAT",
            "LOSS_100_NTBT",
            "LOSS_100_NTAT",
            "LOSS_100_HTBT",
            "LOSS_100_HTAT",
            "LOSS_100_LTBT",
            "LOSS_100_LTAT",
        ],
    ),
    (
        "temperature_rise",
        "Temperature rise",
        "temperature_rise",
        ["time_series.*", "top_oil_rise", "hv_winding_rise", "lv_winding_rise"],
        ["TOP_OIL_RISE", "HV_WINDING_RISE", "LV_WINDING_RISE"],
    ),
    (
        "short_circuit",
        "Short-circuit withstand",
        "short_circuit",
        ["shots.*", "required_*", "condition_before", "during_test", "after_test"],
        ["REACTANCE_NTBT_NTAT", "REACTANCE_HTBT_HTAT", "REACTANCE_LTBT_LTAT", "SC_OVERALL"],
    ),
    (
        "pressure_vacuum",
        "Pressure / vacuum",
        "pressure_oil_leakage",
        ["*pressure*", "*vacuum*", "deflection.*", "type_observation"],
        ["PRESSURE_TEST"],
    ),
    (
        "oil_leakage",
        "Oil leakage",
        "pressure_oil_leakage",
        ["oil_*", "condition_oil"],
        ["OIL_LEAKAGE"],
    ),
]
KNOWN_RULE_CODES = {code for _, _, _, _, codes in TESTS for code in codes}

CROSS_LOSS_ROWS = (
    [
        {
            "kind": "resistance",
            "label": "HV resistance, normal tap",
            "family": "hv_resistance",
            "form_type": "loss_measurement",
            "tap": "principal",
            "unit": "Ω",
            "precision": 4,
        },
        {
            "kind": "resistance",
            "label": "LV resistance",
            "family": "lv_resistance",
            "form_type": "loss_measurement",
            "tap": "0",
            "unit": "mΩ",
            "precision": 4,
        },
        {
            "kind": "ratio",
            "label": "Voltage ratio, normal tap",
            "family": "ratio",
            "form_type": "routine_test",
            "tap": "principal",
            "unit": "ratio",
            "precision": 2,
        },
    ]
    + [
        {
            "kind": "loss",
            "label": f"Total loss {load}% ({tap})",
            "stage": tap,
            "load": load,
            "metric": f"total_loss_{load}",
            "unit": "W",
            "precision": 2,
        }
        for tap, loads in [("NT", ("50", "100")), ("HT", ("100",)), ("LT", ("100",))]
        for load in loads
    ]
    + [
        {
            "kind": "ir",
            "label": label,
            "form_type": "routine_test",
            "prefix": prefix,
            "unit": "GΩ",
            "precision": 2,
        }
        for label, prefix in [
            ("HV-earth IR", "ir_hv_earth"),
            ("LV-earth IR", "ir_lv_earth"),
            ("HV-LV IR", "ir_hv_lv"),
        ]
    ]
)
CROSS_STABILITY_ROWS = [
    {
        "kind": "stability",
        "label": label,
        "stage": tap,
        "metric": metric,
        "unit": "%",
        "precision": 3,
    }
    for label, tap, metric in [
        ("Impedance (principal tap)", "NT", "Z75"),
        ("Reactance (principal tap)", "NT", "X50"),
        ("Reactance (high tap)", "HT", "X50"),
        ("Reactance (low tap)", "LT", "X50"),
    ]
]


def _spec(key, label, unit="", decimals=None):
    return {"key": key, "label": label, "unit": unit, "decimals": decimals}


def report_field_specs(test_id):
    """Exact source cells approved for this fixed report; no extraction-wide lists."""
    if test_id == "winding_resistance":
        specs = []
        for index in range(3):
            specs.append(_spec(f"hv_resistance.{index}.tap", f"HV tap position {index + 1}"))
            for stage in ("BT", "AT"):
                for phase in (1, 2, 3):
                    specs.append(
                        _spec(
                            f"hv_resistance.{index}.{stage}_{phase}",
                            f"HV tap position {index + 1}, {stage} R{phase}",
                            "Ω",
                            4,
                        )
                    )
        for stage in ("BT", "AT"):
            for phase in (1, 2, 3):
                specs.append(
                    _spec(f"lv_resistance.0.{stage}_{phase}", f"LV, {stage} R{phase}", "mΩ", 4)
                )
        return specs
    if test_id == "ratio_vector":
        return [_spec("ratio.{principal}.tap", "Principal tap label")] + [
            _spec(
                f"ratio.{{principal}}.{stage}_{phase}",
                f"Principal tap {stage} phase {phase}",
                "",
                2,
            )
            for stage in ("BT", "AT")
            for phase in "ABC"
        ]
    groups = {
        "insulation_resistance": [
            ("ir_hv_earth", "HV to earth"),
            ("ir_lv_earth", "LV to earth"),
            ("ir_hv_lv", "HV to LV"),
        ],
        "separate_source": [
            ("hv_observation", "HV withstand observation"),
            ("lv_observation", "LV withstand observation"),
        ],
        "induced": [("induced_observation", "Induced withstand observation")],
    }
    if test_id in groups:
        unit = "GΩ" if test_id == "insulation_resistance" else ""
        specs = [
            _spec(f"{key}_{stage}", f'{label}, {"before" if stage == "BT" else "after"} test', unit)
            for key, label in groups[test_id]
            for stage in ("BT", "AT")
        ]
        if test_id == "separate_source":
            for stage in ("BT", "AT"):
                for winding, voltage in (("HV", "hv"), ("LV", "lv")):
                    specs += [
                        _spec(
                            f"{voltage}_voltage_{stage}",
                            f"{winding} applied voltage, {stage}",
                            "kV",
                            2,
                        ),
                        _spec(
                            f"{voltage}_duration_{stage}", f"{winding} duration, {stage}", "s", 0
                        ),
                    ]
            specs += [
                _spec("date_BT", "Routine-test date, before test"),
                _spec("date_AT", "Routine-test date, after test"),
                _spec("instrument_serials", "Routine-test instruments"),
            ]
        if test_id == "induced":
            for stage in ("BT", "AT"):
                specs += [
                    _spec(f"induced_voltage_{stage}", f"Induced applied voltage, {stage}", "V", 0),
                    _spec(f"induced_frequency_{stage}", f"Induced frequency, {stage}", "Hz", 0),
                    _spec(f"induced_duration_{stage}", f"Induced duration, {stage}", "s", 0),
                ]
        return specs
    return {
        "no_load": [
            _spec("no_load_100_percent", "Current at rated voltage", "%", 2),
            _spec("no_load_112_percent", "Current at 112.5% voltage", "%", 2),
            dict(
                _spec("no_load.0.P", "No-load loss, before test", "W", 2),
                form_type="loss_calculation",
            ),
            dict(
                _spec("no_load.1.P", "No-load loss, after test", "W", 2),
                form_type="loss_calculation",
            ),
            _spec("date_BT", "Loss-sheet date, before test"),
            _spec("date_AT", "Loss-sheet date, after test"),
        ],
        "load_loss_impedance": [],
        "total_loss": [],
        "temperature_rise": [
            _spec("top_oil_rise", "Top-oil rise", "K", 2),
            _spec("hv_winding_rise", "HV winding rise", "K", 2),
            _spec("lv_winding_rise", "LV winding rise", "K", 2),
            _spec("test_dates", "Temperature-rise test dates"),
            _spec("instrument_serials", "Temperature-rise instruments"),
        ],
        "short_circuit": [
            _spec("condition_before", "Condition before short circuit"),
            _spec("during_test", "Observation during short circuit"),
            _spec("after_test", "Condition after short circuit"),
            _spec("conductor_core_clamps", "Untanking: conductor, core and clampings"),
            _spec("spacers", "Untanking: spacers"),
            _spec("oil", "Untanking: oil"),
            _spec("date", "Short-circuit test date"),
        ],
        "pressure_vacuum": [
            _spec("routine_pressure", "Applied pressure", "kPa", 2),
            _spec("routine_pressure_observation", "Pressure observation"),
            _spec("type_pressure", "Type pressure", "kPa", 2),
            _spec("type_pressure_duration", "Type-pressure duration", "min", 0),
            _spec("pressure_deflection_result", "Type-pressure deflection result", "mm", 2),
            _spec("vacuum", "Vacuum applied", "mmHg", 0),
            _spec("vacuum_duration", "Vacuum duration", "min", 0),
            _spec("vacuum_deflection_result", "Vacuum deflection result", "mm", 2),
            _spec("pressure_date", "Pressure-test date"),
            _spec("type_date", "Type pressure/vacuum test dates"),
            _spec("instrument_serials", "Pressure/vacuum instruments"),
        ],
        "oil_leakage": [
            _spec("oil_observation", "Oil-leakage observation"),
            _spec("oil_date", "Oil-leakage test date"),
        ],
    }[test_id]


def default_fixed_definition():
    """Seed data; jobs freeze this JSON so later edits cannot change old reports."""
    bindings = {
        key: {"source": "system", "key": key, "unit": "", "decimals": None}
        for key in SYSTEM_KEYS
        if key not in ("cross_test", "cross_test_losses", "cross_test_stability")
    }
    cover_fields = [
        ("customer_name", "customer_request", "customer_name", True),
        ("customer_address", "customer_request", "customer_address", True),
        ("manufacturer", "customer_request", "manufacturer", True),
        ("sample_particulars", "customer_request", "sample_description", True),
        ("rated_power", "transformer_proforma", "rated_power", False),
        ("rated_hv", "transformer_proforma", "rated_hv", False),
        ("rated_lv", "transformer_proforma", "rated_lv", False),
        ("phases", "transformer_proforma", "phases", False),
        ("frequency", "transformer_proforma", "frequency", False),
        ("vector_group", "transformer_proforma", "vector_group", True),
        ("cooling", "transformer_proforma", "cooling", True),
        ("serial_number", "customer_request", "serial_number", True),
        ("drawing_numbers", "customer_request", "drawing_numbers", True),
        ("tests_requested", "customer_request", "requested_test", True),
        ("witness_name", "customer_request", "witness_name", True),
        ("standard", "work_instruction", "standard", True),
        ("assigned_engineer", "work_instruction", "assigned_to", True),
        ("receipt_condition", "customer_request", "sample_suitable", True),
        ("guaranteed_loss_50", "transformer_proforma", "guaranteed_total_loss_50", False),
        ("guaranteed_loss_100", "transformer_proforma", "guaranteed_total_loss_100", False),
        ("guaranteed_impedance", "transformer_proforma", "impedance_at_75", False),
        ("guaranteed_top_oil_rise", "transformer_proforma", "guaranteed_temp_rise_1", False),
        ("guaranteed_winding_rise", "transformer_proforma", "guaranteed_temp_rise_2", False),
    ]
    for key, form_type, field_key, exact_copy in cover_fields:
        bindings[key] = {
            "source": "field",
            "form_type": form_type,
            "key": field_key,
            "unit": "",
            "decimals": None,
            "exact_copy": exact_copy,
        }
    cover = [
        ("Report No.", "{{report_no}}"),
        ("File No.", "{{file_no}}"),
        ("Test series", "{{test_series}}"),
        ("Date of issue", "{{issue_date}}"),
        ("Revision", "{{revision}}"),
        ("Customer", "{{customer_name}}"),
        ("Customer address", "{{customer_address}}"),
        ("Manufacturer", "{{manufacturer}}"),
        ("Sample particulars", "{{sample_particulars}}"),
        (
            "Rating",
            "{{rated_power}} kVA / {{rated_hv}} V / {{rated_lv}} V / {{phases}} Φ / {{frequency}} Hz",
        ),
        ("Vector group", "{{vector_group}}"),
        ("Cooling", "{{cooling}}"),
        ("Serial no.", "{{serial_number}}"),
        ("Drawing nos.", "{{drawing_numbers}}"),
        ("Tests requested", "{{tests_requested}}"),
        ("Witness", "{{witness_name}}"),
        ("Sample code", "{{sample_code}}"),
        ("Date of receipt", "{{receipt_date}}"),
        ("Condition on receipt", "{{receipt_condition}}"),
        ("Test period", "{{test_period}}"),
        ("Standard(s)", "{{standard}}"),
        ("Assigned engineer", "{{assigned_engineer}}"),
    ]
    pages = [
        {
            "id": "cover",
            "title": "TEST REPORT",
            "blocks": [
                {
                    "type": "key_value_table",
                    "title": "Identification and particulars",
                    "rows": cover,
                },
                {"type": "narrative", "title": "OVERALL RESULT", "text": "{{overall_result}}"},
                {"type": "signature_block", "title": "Authorisation", "value": "{{signatures}}"},
            ],
        },
        {
            "id": "summary",
            "title": "SUMMARY OF RESULTS",
            "blocks": [
                {
                    "type": "verdict_table",
                    "value": "{{summary_rows}}",
                    "columns": [
                        "Sl",
                        "Test",
                        "Clause / basis",
                        "Requirement",
                        "Measured",
                        "Margin",
                        "Result",
                    ],
                },
            ],
        },
        {
            "id": "sample",
            "title": "SAMPLE DESCRIPTION",
            "blocks": [
                {
                    "type": "key_value_table",
                    "rows": [
                        ("Rated power", "{{rated_power}} kVA"),
                        ("HV / LV", "{{rated_hv}} V / {{rated_lv}} V"),
                        ("Phases / frequency", "{{phases}} Φ / {{frequency}} Hz"),
                        ("Vector group / cooling", "{{vector_group}} / {{cooling}}"),
                        ("Serial no.", "{{serial_number}}"),
                        (
                            "50% / 100% guaranteed losses",
                            "{{guaranteed_loss_50}} W / {{guaranteed_loss_100}} W",
                        ),
                        ("Guaranteed impedance", "{{guaranteed_impedance}} %"),
                        (
                            "Guaranteed top-oil / winding rise",
                            "{{guaranteed_top_oil_rise}} K / {{guaranteed_winding_rise}} K",
                        ),
                    ],
                },
            ],
        },
    ]
    formulas = {
        "winding_resistance": "Phase mean = (R1 + R2 + R3) / 3.",
        "ratio_vector": "Phase mean = (A + B + C) / 3; change = (after − before) / before × 100%.",
        "insulation_resistance": "Before and after insulation readings are compared descriptively.",
        "separate_source": "Applied voltage, duration and observation are reported as recorded.",
        "induced": "Applied voltage, frequency, duration and observation are reported as recorded.",
        "no_load": "No-load current percentage and recorded permissible limit are compared by the configured rule.",
        "load_loss_impedance": "R75 = Rt × 310 / (235 + t); load loss separates copper and stray components.",
        "total_loss": "Total loss = no-load loss + load loss at the stated load fraction.",
        "temperature_rise": "Winding rise uses the resistance method; recorded rise is compared with the guaranteed limit.",
        "short_circuit": "Signed reactance change = (X BT − X AT) / X BT × 100%, using X rounded to 3 dp; circular-coil check compares |change| with 2%.",
        "pressure_vacuum": "Deflection difference = reading during test − reading before test.",
        "oil_leakage": "Oil-leakage outcome follows the recorded pressure, duration and observation.",
    }
    for number, (test_id, title, form_type, patterns, codes) in enumerate(TESTS, 1):
        for suffix, source in [
            ("conditions", "fields"),
            ("readings", "fields"),
            ("calculated", "calculated"),
            ("verdicts", "rules"),
            ("conclusion", "conclusion"),
            ("remark", "remark"),
        ]:
            bindings[f"{suffix}.{test_id}"] = {
                "source": source,
                "form_type": form_type,
                "patterns": patterns,
                "codes": codes,
                "test_id": test_id,
                "unit": "",
                "decimals": None,
            }
        bindings[f"readings.{test_id}"]["field_specs"] = report_field_specs(test_id)
        bindings[f"conditions.{test_id}"]["field_specs"] = []
        metric_sets = {
            "load_loss_impedance": [
                ("load_loss_100", "Load loss at 75 °C", "W", 2),
                ("Z75", "Impedance at 75 °C", "%", 3),
            ],
            "total_loss": [
                ("total_loss_50", "Total loss at 50%", "W", 2),
                ("total_loss_100", "Total loss at 100%", "W", 2),
            ],
            "short_circuit": [("X50", "Reactance", "%", 3)],
        }
        bindings[f"calculated.{test_id}"]["metrics"] = [
            {"key": key, "label": label, "unit": unit, "decimals": decimals}
            for key, label, unit, decimals in metric_sets.get(test_id, [])
        ]
        readings_columns = ["Reading", "Recorded value", "Unit", "Review", "Source"]
        if test_id in ("winding_resistance", "ratio_vector"):
            readings_columns = (
                [
                    "Tap / winding",
                    "BT R1",
                    "BT R2",
                    "BT R3",
                    "AT R1",
                    "AT R2",
                    "AT R3",
                    "Unit",
                    "Review",
                ]
                if test_id == "winding_resistance"
                else ["Tap", "BT A", "BT B", "BT C", "AT A", "AT B", "AT C", "Unit", "Review"]
            )
        blocks = [
            {
                "type": "header_fields",
                "title": "Test conditions",
                "value": "{{conditions." + test_id + "}}",
            },
            {
                "type": "data_table",
                "title": "Readings",
                "value": "{{readings." + test_id + "}}",
                "columns": readings_columns,
            },
            {
                "type": "data_table",
                "title": "Calculated results",
                "value": "{{calculated." + test_id + "}}",
                "columns": ["Parameter", "Value", "Unit", "Method / source"],
            },
            {"type": "narrative", "title": "Formula / method", "text": formulas[test_id]},
            {
                "type": "verdict_table",
                "title": "Engineering verdict",
                "value": "{{verdicts." + test_id + "}}",
                "columns": ["Check", "Measured", "Limit / tolerance", "Margin", "Result"],
            },
            {"type": "narrative", "title": "Conclusion", "text": "{{conclusion." + test_id + "}}"},
            {
                "type": "narrative",
                "title": "Engineer's remark",
                "text": "{{remark." + test_id + "}}",
            },
        ]
        if test_id == "temperature_rise":
            bindings["heating_curve"] = {
                "source": "chart",
                "key": "heating_curve",
                "unit": "°C",
                "decimals": 1,
                "series_indices": [0, 3, 6, 9, 12],
                "elapsed_hours": [0, 3, 6, 9, 12],
                "series_keys": ["top_oil", "bottom_oil", "ambient_1", "ambient_2", "ambient_3"],
            }
            blocks.insert(
                4, {"type": "chart", "title": "Heating curve", "value": "{{heating_curve}}"}
            )
        if test_id == "short_circuit":
            bindings["before_after_sc"] = {
                "source": "system",
                "key": "cross_test_stability",
                "unit": "",
                "decimals": None,
                "row_specs": deepcopy(CROSS_STABILITY_ROWS),
            }
            blocks.insert(
                4,
                {
                    "type": "data_table",
                    "title": "Before / after short circuit",
                    "value": "{{before_after_sc}}",
                    "columns": [
                        "Parameter",
                        "Before",
                        "After",
                        "Unit",
                        "Change: (BT−AT)/BT × 100% (3 dp)",
                    ],
                },
            )
        pages.append({"id": test_id, "title": f"{number}. {title.upper()}", "blocks": blocks})
    pages += [
        {
            "id": "cross_test",
            "title": "CROSS-TEST CONSISTENCY",
            "blocks": [
                {
                    "type": "data_table",
                    "title": "Routine and loss comparisons",
                    "value": "{{cross_test_losses}}",
                    "columns": ["Parameter", "Before", "After", "Unit", "Change: AT−BT (W or %)"],
                },
                {
                    "type": "data_table",
                    "title": "Impedance and reactance stability",
                    "value": "{{cross_test_stability}}",
                    "columns": [
                        "Parameter",
                        "Before",
                        "After",
                        "Unit",
                        "Change: (BT−AT)/BT × 100% (3 dp)",
                    ],
                },
            ],
        },
        {
            "id": "observations",
            "title": "OBSERVATIONS & REMARKS",
            "blocks": [
                {"type": "annexure_list", "value": "{{observations}}"},
            ],
        },
        {
            "id": "annexures",
            "title": "ANNEXURES",
            "blocks": [
                {
                    "type": "data_table",
                    "title": "A. Source records",
                    "value": "{{source_records}}",
                    "columns": ["Source document", "Page", "Date"],
                },
                {"type": "annexure_list", "title": "B. Instruments", "value": "{{instruments}}"},
                {
                    "type": "annexure_list",
                    "title": "C. Data-review & change summary",
                    "value": "{{review_summary}}",
                },
                {
                    "type": "data_table",
                    "title": "D. Rule provenance",
                    "value": "{{rule_provenance}}",
                    "columns": ["Engineering check", "Clause / basis", "Status"],
                },
                {
                    "type": "annexure_list",
                    "title": "E. Abbreviations",
                    "value": "{{abbreviations}}",
                },
                {"type": "narrative", "text": "— End of Report —"},
            ],
        },
    ]
    bindings["cross_test_losses"] = {
        "source": "system",
        "key": "cross_test_losses",
        "unit": "",
        "decimals": None,
        "row_specs": deepcopy(CROSS_LOSS_ROWS),
    }
    bindings["cross_test_stability"] = {
        "source": "system",
        "key": "cross_test_stability",
        "unit": "",
        "decimals": None,
        "row_specs": deepcopy(CROSS_STABILITY_ROWS),
    }
    return {
        "format": TEMPLATE_NAME,
        "report_title": "Transformer test report",
        "pages": pages,
        "bindings": bindings,
    }


def validate_fixed_definition(definition):
    if not isinstance(definition, dict) or definition.get("format") != TEMPLATE_NAME:
        raise ValidationError("A fixed template needs its format identifier.")
    pages, bindings = definition.get("pages"), definition.get("bindings")
    if not isinstance(pages, list) or not isinstance(bindings, dict):
        raise ValidationError("A fixed template needs pages and bindings.")
    required = (
        ["cover", "summary", "sample"]
        + [row[0] for row in TESTS]
        + ["cross_test", "observations", "annexures"]
    )
    if [page.get("id") for page in pages] != required:
        raise ValidationError("The CPRI-SCL-TR-v1 section order is fixed.")
    placeholders = set()
    for page in pages:
        if not isinstance(page.get("title"), str) or not isinstance(page.get("blocks"), list):
            raise ValidationError("Every page needs a title and blocks.")
        for block in page["blocks"]:
            if block.get("type") not in BLOCK_TYPES:
                raise ValidationError("Unknown report block type.")
            for value in list(block.values()) + [
                item for row in block.get("rows", []) for item in row
            ]:
                if isinstance(value, str):
                    placeholders.update(PLACEHOLDER.findall(value))
    if placeholders != set(bindings):
        raise ValidationError(
            "Every placeholder must have exactly one binding, with no unused bindings."
        )
    known = {
        schema["form_type"]: {field["key"] for field in schema["fields"]} for schema in schemas()
    }
    for key, binding in bindings.items():
        if not isinstance(binding, dict) or binding.get("source") not in {
            "system",
            "field",
            "fields",
            "rules",
            "calculated",
            "conclusion",
            "remark",
            "chart",
        }:
            raise ValidationError(f"{key}: unknown mapping source.")
        if "unit" not in binding or "decimals" not in binding:
            raise ValidationError(f"{key}: unit and decimals must be defined.")
        source = binding["source"]
        if source == "system" and binding.get("key") not in SYSTEM_KEYS:
            raise ValidationError(f"{key}: unknown system value.")
        if source == "system" and "row_specs" in binding:
            if any(
                not spec.get("label")
                or "unit" not in spec
                or spec.get("kind") not in ("resistance", "ratio", "loss", "ir", "stability")
                for spec in binding["row_specs"]
            ):
                raise ValidationError(
                    f"{key}: cross-test rows require a mapped kind, label and unit."
                )
        if source == "field":
            if binding.get("key") not in known.get(binding.get("form_type"), set()):
                raise ValidationError(f"{key}: unknown source field.")
            if key in (
                "customer_name",
                "customer_address",
                "manufacturer",
                "sample_particulars",
                "serial_number",
                "drawing_numbers",
                "tests_requested",
                "witness_name",
            ) and not binding.get("exact_copy"):
                raise ValidationError(f"{key}: customer text must be exact-copy.")
        if source == "fields":
            available = known.get(binding.get("form_type"), set())
            if not binding.get("patterns") or any(
                not any(fnmatch.fnmatchcase(name, pattern) for name in available)
                for pattern in binding["patterns"]
            ):
                raise ValidationError(f"{key}: unknown source field pattern.")
            if "field_specs" in binding:
                for spec in binding["field_specs"]:
                    actual = spec.get("key", "").replace("{principal}", "0")
                    own_available = known.get(
                        spec.get("form_type", binding.get("form_type")), set()
                    )
                    if actual not in own_available or not spec.get("label") or "unit" not in spec:
                        raise ValidationError(
                            f"{key}: every printed field needs a mapped key, human label and unit."
                        )
        if source == "chart" and "series_indices" in binding:
            available = known.get("temperature_rise", set())
            if any(
                f"time_series.{index}.{series}" not in available
                for index in binding["series_indices"]
                for series in binding["series_keys"]
            ):
                raise ValidationError(f"{key}: chart point is not mapped to the source schema.")
        if source == "rules":
            codes = binding.get("codes")
            if not isinstance(codes, list) or not set(codes).issubset(KNOWN_RULE_CODES):
                raise ValidationError(f"{key}: unknown rule code.")
    return True


def clone_with_binding(definition, key, replacement):
    updated = deepcopy(definition)
    if key not in updated["bindings"]:
        raise ValidationError("Unknown placeholder.")
    updated["bindings"][key] = replacement
    validate_fixed_definition(updated)
    return updated


def crf_exact_copy_mismatches(data):
    """Compare frozen CRF transcriptions with the customer-submitted request."""
    definition = (data.get("template_mapping") or {}).get("definition") or {}
    if definition.get("format") != TEMPLATE_NAME:
        return []
    request = data.get("request_snapshot") or {}
    names = {
        "customer_name": "customer",
        "customer_address": "customer_address",
        "manufacturer": "manufacturer",
        "sample_particulars": "sample_particulars",
        "tests_requested": "requested_tests",
    }
    mismatches = []
    for placeholder, request_key in names.items():
        binding = definition["bindings"][placeholder]
        expected = request.get(request_key)
        if not expected:
            continue
        matches = [
            field
            for field in data.get("fields", [])
            if field.get("form_type") == binding["form_type"]
            and field.get("schema_key") == binding["key"]
            and str(field.get("value") or "").strip()
        ]
        if len(matches) != 1 or matches[0]["value"] != str(expected):
            mismatches.append(placeholder)
    return mismatches
