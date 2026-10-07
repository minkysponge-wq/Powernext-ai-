"""Golden checks for the v6 fixed report and v2 engineering corrections."""

from copy import deepcopy
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from .calculations import evaluate_rule
from .fixed_report import (
    _comparison,
    _cross_test_rows,
    _empty_chart_message,
    _mapped_readings,
    _resolve,
    _rule_rows,
    _system,
    _test_summary,
    mapped_review_rows,
)
from .fixed_template import default_fixed_definition
from .principal_tap import nominal_phase_ratio, principal_row_prefix
from .test_rule_v2 import evidence, rule_library
from .transformer import calculate_datasheet


class FixedReportGoldenTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.definition = default_fixed_definition()
        cls.rules = rule_library()

    def snapshot(self, fields=None, rows=None, results=None):
        return {
            "fields": fields or [],
            "transformer_calculations": [{"source": "Reviewed worksheet", "rows": rows or []}],
            "calculations": results or [],
            "template_mapping": {"version": 7, "definition": self.definition},
            "documents": [],
        }

    def test_a1_rounding_grid_reproduces_frozen_synthetic_printed_values(self):
        path = Path(__file__).resolve().parents[2] / "benchmark" / "rounding_search_v217.py"
        spec = spec_from_file_location("rounding_search_v217", path)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.search()
        self.assertEqual(result["combinations_tried"], 2304)
        self.assertGreater(result["exact_match_count"], 0)
        self.assertEqual(result["baseline_gap_w"]["NTBT total_loss_50"], 0.0)
        self.assertEqual(result["baseline_gap_w"]["NTAT total_loss_100"], 0.0)
        self.assertEqual(result["closest_gap_w"]["LTAT total_loss_100"], 0.0)

    def test_a2_principal_tap_is_resolved_from_label_not_array_index(self):
        fields, _ = evidence()
        next(f for f in fields if f["schema_key"] == "ratio.3.tap")["value"] = "4"
        fields.append(
            {
                "key": "routine_test.ratio.2.tap",
                "form_type": "routine_test",
                "schema_key": "ratio.2.tap",
                "value": "3",
                "unit": "",
                "status": "verified",
            }
        )
        for stage in ("BT", "AT"):
            for phase in "ABC":
                fields.append(
                    {
                        "key": f"ratio.2.{stage}_{phase}",
                        "form_type": "routine_test",
                        "schema_key": f"ratio.2.{stage}_{phase}",
                        "value": "44.00",
                        "unit": "",
                        "status": "verified",
                    }
                )
        self.assertEqual(principal_row_prefix(fields, "routine_test", "ratio"), "ratio.2")
        result = evaluate_rule(self.rules["VOLTAGE_RATIO"], fields, evidence()[1], {})
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["tap_label"], "3")
        self.assertEqual(Decimal(result["before_ratio"]), Decimal("44.00"))

    def test_a2_hv_resistance_uses_n_label_for_bt_and_at(self):
        fields, _ = evidence()
        next(f for f in fields if f["schema_key"] == "hv_resistance.1.tap")["value"] = "H"
        fields.append(
            {
                "form_type": "loss_measurement",
                "schema_key": "hv_resistance.0.tap",
                "value": "N",
                "status": "verified",
                "unit": "",
            }
        )
        for stage, values in [("BT", (3.9012, 3.9134, 3.9256)), ("AT", (3.9412, 3.9534, 3.9656))]:
            for phase, value in enumerate(values, 1):
                fields.append(
                    {
                        "form_type": "loss_measurement",
                        "schema_key": f"hv_resistance.0.{stage}_{phase}",
                        "value": str(value),
                        "status": "verified",
                        "unit": "Ω",
                    }
                )
        row = next(
            row
            for row in _cross_test_rows(self.snapshot(fields), "cross_test_losses")
            if row[0] == "HV resistance, normal tap"
        )
        self.assertEqual(row[1:4], ["3.9134", "3.9534", "Ω"])

    def test_a3_differences_come_from_displayed_values(self):
        self.assertEqual(
            _comparison(917.9552, 915.7349, "W", 2, "change"), ("917.96", "915.73", "-2.23")
        )
        self.assertEqual(
            _comparison(2423.0748, 2419.7963, "W", 2, "change"), ("2423.07", "2419.80", "-3.27")
        )

    def test_a4_impedance_and_reactance_share_bt_minus_at_convention(self):
        data = self.snapshot(
            rows=[
                {"stage": "NTBT", "Z75": 4.335, "X50": 3.680},
                {"stage": "NTAT", "Z75": 4.355, "X50": 3.686},
            ]
        )
        rows = _cross_test_rows(data, "cross_test_stability")
        self.assertEqual(rows[0][4], "-0.461")
        self.assertEqual(rows[1][4], "-0.163")
        block = next(
            block
            for page in self.definition["pages"]
            if page["id"] == "cross_test"
            for block in page["blocks"]
            if block.get("value") == "{{cross_test_stability}}"
        )
        self.assertIn("(BT−AT)/BT", block["columns"][-1])

    def test_a5_lt_mapping_uses_its_own_before_and_after_inputs(self):
        import json

        values = json.loads(
            (
                Path(__file__).resolve().parent / "fixtures" / "synthetic_datasheet_v217.json"
            ).read_text()
        )["values"]
        original = {row["stage"]: row for row in calculate_datasheet(values)}
        changed = deepcopy(values)
        changed["load_measurement.5.P1"] = str(float(changed["load_measurement.5.P1"]) + 10)
        altered = {row["stage"]: row for row in calculate_datasheet(changed)}
        self.assertEqual(original["LTBT"]["total_loss_100"], altered["LTBT"]["total_loss_100"])
        self.assertNotEqual(original["LTAT"]["total_loss_100"], altered["LTAT"]["total_loss_100"])
        self.assertEqual(
            _comparison(
                original["LTBT"]["total_loss_100"],
                original["LTAT"]["total_loss_100"],
                "W",
                2,
                "change",
            )[2],
            "-3.29",
        )

    def test_a6_active_tap_rule_is_descriptive_without_declaration(self):
        fields, calculations = evidence()
        rule = self.rules["IMPEDANCE_HTBT"]
        self.assertEqual(rule.parameters["v2_status"], "ACTIVE")
        result = evaluate_rule(rule, fields, calculations, {})
        self.assertEqual(result["verdict"], "descriptive")
        self.assertEqual(result["v2_status"], "DESCRIPTIVE")
        self.assertIn(
            "4.335",
            _rule_rows({"calculations": [result]}, {"codes": ["IMPEDANCE_HTBT"]}, False)[0][1],
        )

    def test_a7_ratio_uses_dyn_nameplate_and_checks_both_stages(self):
        fields, calculations = evidence()
        nominal, _ = nominal_phase_ratio(fields)
        self.assertAlmostEqual(float(nominal), 43.999, delta=0.02)
        result = evaluate_rule(self.rules["VOLTAGE_RATIO"], fields, calculations, {})
        self.assertEqual(result["verdict"], "pass")
        self.assertIn("before_deviation", result)
        self.assertIn("after_deviation", result)
        self.assertIn("[CLAUSE TBC]", result["limit_provenance"])
        self.assertEqual(self.rules["VOLTAGE_RATIO"].parameters["ratio_max_percent"], "0.5")
        self.assertIn(
            "[CLAUSE TBC]",
            _rule_rows({"calculations": [result]}, {"codes": ["VOLTAGE_RATIO"]}, False)[0][2],
        )

    def test_b8_readings_use_only_mapped_cells_and_full_resistance_grid(self):
        binding = self.definition["bindings"]["readings.winding_resistance"]
        data = self.snapshot(
            [
                {
                    "form_type": "loss_measurement",
                    "schema_key": "hv_resistance.0.tap",
                    "value": "N",
                    "status": "verified",
                    "unit": "",
                },
                {
                    "form_type": "loss_measurement",
                    "schema_key": "unmapped_secret",
                    "value": "DO NOT PRINT",
                    "status": "verified",
                    "unit": "",
                },
            ]
        )
        rows = _mapped_readings(data, binding)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(rows[0]), 9)
        self.assertEqual(rows[0][0], "HV normal tap")
        self.assertNotIn("DO NOT PRINT", str(rows))
        self.assertNotIn("hv_resistance.", str(rows))

    def test_b8_cross_test_rows_follow_frozen_mapping(self):
        binding = deepcopy(self.definition["bindings"]["cross_test_stability"])
        binding["row_specs"] = binding["row_specs"][:1]
        data = self.snapshot(
            rows=[
                {"stage": "NTBT", "Z75": 4.335, "X50": 3.680},
                {"stage": "NTAT", "Z75": 4.355, "X50": 3.686},
            ]
        )
        rows = _resolve(
            data,
            "cross_test_stability",
            binding,
            SimpleNamespace(revision=1, approved_at=None),
            False,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Impedance (principal tap)")

    def test_b9_pending_value_and_chart_are_suppressed(self):
        binding = self.definition["bindings"]["readings.induced"]
        data = self.snapshot(
            [
                {
                    "form_type": "routine_test",
                    "schema_key": "induced_observation_BT",
                    "value": "No disruptive discharg",
                    "status": "unreviewed",
                    "unit": "",
                },
                {
                    "form_type": "temperature_rise",
                    "schema_key": "time_series.0.top_oil",
                    "value": "99.9",
                    "status": "unreviewed",
                    "unit": "°C",
                },
            ]
        )
        rows = _mapped_readings(data, binding)
        self.assertEqual(rows[0][1], "[pending review]")
        self.assertNotIn("No disruptive discharg", str(rows))
        chart = _resolve(
            data,
            "heating_curve",
            self.definition["bindings"]["heating_curve"],
            SimpleNamespace(revision=1, approved_at=None),
            False,
        )
        self.assertEqual(chart, [])
        self.assertEqual(
            _empty_chart_message(SimpleNamespace(snapshot=data)), "Heating curve: [pending review]."
        )
        data["documents"] = [{"id": "doc-1", "name": "Routine log"}]
        data["fields"].append(
            {
                "form_type": "routine_test",
                "schema_key": "date_BT",
                "value": "29-10-25",
                "status": "unreviewed",
                "document_id": "doc-1",
                "page": 1,
            }
        )
        self.assertEqual(
            _system(data, "source_records", SimpleNamespace(), False)[0][2], "[pending review]"
        )

    def test_b10_units_are_normalised_and_not_doubled(self):
        binding = self.definition["bindings"]["readings.insulation_resistance"]
        data = self.snapshot(
            [
                {
                    "form_type": "routine_test",
                    "schema_key": "ir_hv_earth_BT",
                    "value": "1.689GΉ",
                    "status": "verified",
                    "unit": "GΉ",
                }
            ]
        )
        row = _mapped_readings(data, binding)[0]
        self.assertEqual(row[1:3], ["1.689", "GΩ"])
        self.assertNotIn("GΉ", str(row))

    def test_b11_margins_and_changes_are_at_most_two_decimals(self):
        rule = {
            "code": "TOP_OIL_RISE",
            "title": "Top oil rise",
            "version": 2,
            "verdict": "marginal",
            "value": "39.876",
            "unit": "K",
            "limit": "40",
            "margin": "0.1249",
            "margin_unit": "K",
            "parameters": {"v2_kind": "temperature"},
        }
        row = _rule_rows({"calculations": [rule]}, {"codes": ["TOP_OIL_RISE"]}, False)[0]
        self.assertEqual(row[3], "0.12 K")
        self.assertEqual(_comparison(4.9008, 4.9068, "%", 3, "stability")[2], "-0.122")

    def test_b12_summary_deduplicates_observation(self):
        rule = {
            "code": "SEPARATE_SOURCE_AC",
            "title": "Separate source",
            "version": 2,
            "v2_status": "OBSERVATION",
            "verdict": "pass",
            "value": "Withstood; Withstood; Withstood",
            "unit": "",
            "source_clause": "clause TBC",
            "inputs": [],
        }
        data = {"calculations": [rule]}
        row = _test_summary(
            data, "separate_source", "Separate source", ["SEPARATE_SOURCE_AC"], False
        )
        self.assertEqual(row[3], "Withstood (BT and AT)")
        self.assertNotIn(";", row[3])

    def test_b13_uncited_temperature_limit_is_tbc_in_annex(self):
        rule = {
            "code": "TOP_OIL_RISE",
            "title": "Top oil rise",
            "version": 2,
            "rule_status": "assumed",
            "source_clause": "IS 2026 (Part 2):2010 method; limits from IS 1180 (Part 1); clause TBC",
            "parameters": {"v2_kind": "temperature"},
            "limit_provenance": "manufacturer guarantee 40 K stricter than IS limit 45 K",
        }
        rows = _system({"calculations": [rule]}, "rule_provenance", SimpleNamespace(), False)
        self.assertIn("IS limit: [TBC]", rows[0][1])
        self.assertNotIn("45 K", rows[0][1])

    def test_review_list_contains_only_template_mapped_unreviewed_cells(self):
        data = self.snapshot(
            [
                {
                    "form_type": "routine_test",
                    "schema_key": "ir_hv_earth_BT",
                    "value": "1.2",
                    "status": "unreviewed",
                    "unit": "GΩ",
                },
                {
                    "form_type": "routine_test",
                    "schema_key": "unmapped_secret",
                    "value": "NO",
                    "status": "unreviewed",
                    "unit": "",
                },
            ]
        )
        rows = mapped_review_rows(data)
        self.assertTrue(any(label == "HV to earth, before test" for _, label, _, _ in rows))
        self.assertFalse(any(value == "NO" for _, _, value, _ in rows))

    def test_synthetic_v217_losses_impedance_and_method(self):
        import json

        values = json.loads(
            (
                Path(__file__).resolve().parent / "fixtures" / "synthetic_datasheet_v217.json"
            ).read_text()
        )["values"]
        rows = calculate_datasheet(values)
        printed = {
            ("NTBT", "total_loss_50"): 917.94,
            ("NTAT", "total_loss_50"): 915.55,
            ("NTBT", "total_loss_100"): 2473.97,
            ("NTAT", "total_loss_100"): 2470.48,
            ("HTBT", "total_loss_100"): 2423.27,
            ("HTAT", "total_loss_100"): 2419.94,
            ("LTBT", "total_loss_100"): 2642.74,
            ("LTAT", "total_loss_100"): 2639.45,
        }
        by_stage = {row["stage"]: row for row in rows}
        for (stage, metric), target in printed.items():
            with self.subTest(stage=stage, metric=metric):
                self.assertLessEqual(abs(round(by_stage[stage][metric], 2) - target), 0.011)
        self.assertEqual(round(by_stage["NTBT"]["Z75"], 3), 4.335)
        self.assertEqual(round(by_stage["LTAT"]["Z75"], 3), 4.471)
        data = self.snapshot(rows=rows)
        rendered = _resolve(
            data,
            "calculated.total_loss",
            self.definition["bindings"]["calculated.total_loss"],
            SimpleNamespace(approved_at=None),
            False,
        )
        self.assertTrue(rendered)
        self.assertTrue(all(row[3] == "Calculated by VectorLab (v2.17 method)" for row in rendered))

    def test_final_review_2_checked_no_load_and_ratio_bindings(self):
        fields, calcs = evidence()
        for key in ("rated_hv", "rated_lv"):
            next(
                f
                for f in fields
                if f["form_type"] == "transformer_proforma" and f["schema_key"] == key
            )["status"] = "unreviewed"
            fields.append(
                {
                    "key": "loss_calculation." + key,
                    "form_type": "loss_calculation",
                    "schema_key": key,
                    "value": "11000" if key == "rated_hv" else "433",
                    "unit": "V",
                    "status": "verified",
                }
            )
        for key in ("tap_range", "tap_step"):
            next(f for f in fields if f["schema_key"] == key)["status"] = "unreviewed"
        no_load = evaluate_rule(
            self.rules["NO_LOAD_CURRENT_112"],
            fields,
            calcs,
            {"requested_tests_text": "No-load current at 112.5%"},
        )
        ratio = evaluate_rule(self.rules["VOLTAGE_RATIO"], fields, calcs, {})
        self.assertEqual((no_load["verdict"], no_load["value"]), ("pass", "1.31"))
        self.assertEqual((ratio["verdict"], ratio["tap_label"]), ("pass", "3"))
        self.assertAlmostEqual(float(ratio["nominal_ratio"]), 11000 / (433 / (3**0.5)), places=2)
        self.assertAlmostEqual(float(ratio["limit"]), min(0.5, 4.335 / 10), places=2)
        cross = next(
            row
            for row in _cross_test_rows(self.snapshot(fields), "cross_test_losses")
            if row[0] == "Voltage ratio, normal tap"
        )
        self.assertEqual(cross[1:3], ["44.00", "44.01"])

    def test_final_review_3_stability_precision_and_margin(self):
        self.assertEqual(
            _comparison(3.6804, 3.6856, "%", 3, "stability"), ("3.680", "3.686", "-0.163")
        )
        rule = {
            "code": "VOLTAGE_RATIO",
            "title": "Ratio",
            "version": 2,
            "verdict": "pass",
            "value": "0.201606",
            "unit": "%",
            "limit": "0.438081",
            "margin": "0.236475",
            "margin_unit": "%",
            "parameters": {"v2_kind": "ratio"},
        }
        self.assertEqual(
            _rule_rows({"calculations": [rule]}, {"codes": ["VOLTAGE_RATIO"]}, False)[0][3],
            "0.24 %",
        )

    def test_final_review_4_heating_curve_hours_and_axis_titles(self):
        from io import BytesIO

        from pypdf import PdfReader

        from .fixed_report import render_fixed_pdf

        fields = []
        for index in (0, 3, 6, 9, 12):
            for key, value in (
                ("top_oil", 40),
                ("bottom_oil", 35),
                ("ambient_1", 25),
                ("ambient_2", 25),
                ("ambient_3", 25),
            ):
                fields.append(
                    {
                        "form_type": "temperature_rise",
                        "schema_key": f"time_series.{index}.{key}",
                        "value": str(value),
                        "status": "verified",
                        "unit": "°C",
                    }
                )
        data = self.snapshot(fields)
        chart = _resolve(
            data,
            "heating_curve",
            self.definition["bindings"]["heating_curve"],
            SimpleNamespace(approved_at=None),
            False,
        )
        self.assertEqual([row[0] for row in chart], [0, 3, 6, 9, 12])
        report = SimpleNamespace(
            snapshot=data,
            approved_at=None,
            revision=1,
            pk="draft",
            engineer_locked_by=None,
            quality_verified_by=None,
            approved_by=None,
        )
        text = "\n".join(
            page.extract_text() or "" for page in PdfReader(BytesIO(render_fixed_pdf(report))).pages
        )
        self.assertIn("Elapsed time (h)", text)
        self.assertIn("Temperature (°C)", text)

    def test_final_review_5_annex_rule_and_mapped_counts(self):
        fields = [
            {
                "form_type": "routine_test",
                "schema_key": "ir_hv_earth_BT",
                "value": "1.2",
                "status": "verified",
                "unit": "GΩ",
            },
            {
                "form_type": "routine_test",
                "schema_key": "ir_hv_earth_AT",
                "value": "1.3",
                "status": "unreviewed",
                "unit": "GΩ",
            },
        ]
        data = self.snapshot(fields)
        counts = _system(data, "review_summary", SimpleNamespace(), False)
        total = len(mapped_review_rows(data, include_verified=True))
        pending = len(mapped_review_rows(data))
        self.assertIn(f"Template-bound checked readings: {total-pending}", counts[0])
        self.assertIn(f"Template-bound readings awaiting review: {pending}", counts[0])
        data["calculations"] = [
            {
                "code": "TOP_OIL_RISE",
                "title": "Top oil rise",
                "version": 2,
                "source_clause": "IS 2026-2 method",
                "parameters": {"v2_kind": "temperature"},
                "limit_provenance": "manufacturer guarantee 35 K stricter than cited IS limit 45 K",
            }
        ]
        annex = _system(data, "rule_provenance", SimpleNamespace(), False)
        self.assertIn("IS limit: [TBC]", annex[0][1])
        self.assertNotIn("stricter than", str(annex))

    def test_final_review_6_cover_uses_confirmed_proforma_scope(self):
        requested = (
            "Dynamic ability to withstand short circuit (with thermal short); "
            "Temperature rise test (35°/40°C); Pressure test; Vacuum test; "
            "No-load current at 112.5%; Oil leakage test"
        )
        data = self.snapshot()
        data["source_checked_cover"] = {"tests_requested": requested}
        value = _resolve(
            data,
            "tests_requested",
            self.definition["bindings"]["tests_requested"],
            SimpleNamespace(approved_at=None),
            False,
        )
        self.assertEqual(value, requested)
        self.assertNotIn("Unbalance Current", value)

    def test_final_review_7_new_fields_are_mapped_and_pending(self):
        expected = {
            "hv_voltage_BT",
            "lv_voltage_BT",
            "hv_duration_BT",
            "induced_voltage_BT",
            "induced_frequency_BT",
            "induced_duration_BT",
            "no_load.0.P",
            "conductor_core_clamps",
            "spacers",
            "oil",
            "type_pressure",
            "type_pressure_duration",
            "pressure_deflection_result",
            "vacuum",
            "vacuum_duration",
            "vacuum_deflection_result",
            "date_BT",
            "test_dates",
            "instrument_serials",
        }
        keys = {
            spec["key"]
            for key, binding in self.definition["bindings"].items()
            if key.startswith("readings.")
            for spec in binding.get("field_specs", [])
        }
        self.assertTrue(expected.issubset(keys))
        data = self.snapshot(
            [
                {
                    "form_type": "pressure_oil_leakage",
                    "schema_key": "type_pressure",
                    "value": "80",
                    "status": "unreviewed",
                    "unit": "kPa",
                    "document_id": "doc",
                    "page": 1,
                }
            ]
        )
        data["documents"] = [{"id": "doc", "name": "Pressure logsheet.pdf"}]
        review = mapped_review_rows(data)
        self.assertTrue(
            any(
                label == "Vacuum deflection result" and "Pressure logsheet.pdf, p. 1" in source
                for _, label, _, source in review
            )
        )
        rows = _mapped_readings(data, self.definition["bindings"]["readings.pressure_vacuum"])
        self.assertIn("[pending review]", str(rows))
        self.assertNotIn("'80'", str(rows))
        dated = self.snapshot(
            [
                {
                    "form_type": "temperature_rise",
                    "schema_key": "test_dates",
                    "value": "9 & 10/11/25",
                    "status": "unreviewed",
                    "unit": "",
                    "document_id": "temp",
                    "page": 1,
                }
            ]
        )
        dated["documents"] = [{"id": "temp", "name": "Logsheet for temp. rise.pdf"}]
        source_rows = _mapped_readings(
            dated, self.definition["bindings"]["readings.temperature_rise"]
        )
        self.assertIn("Temperature-rise logsheet, p. 1", str(source_rows))

    def test_final_review_8_observation_and_untanking_gate(self):
        fields, calcs = evidence()
        next(f for f in fields if f["schema_key"] == "routine_pressure_observation")[
            "value"
        ] = "No leakage at any point"
        next(f for f in fields if f["schema_key"] == "oil_observation")[
            "value"
        ] = "No leakage at any point"
        self.assertEqual(
            evaluate_rule(self.rules["PRESSURE_TEST"], fields, calcs, {})["verdict"], "pass"
        )
        self.assertEqual(
            evaluate_rule(self.rules["OIL_LEAKAGE"], fields, calcs, {})["verdict"], "pass"
        )
        pressure = evaluate_rule(self.rules["PRESSURE_TEST"], fields, calcs, {})
        oil = evaluate_rule(self.rules["OIL_LEAKAGE"], fields, calcs, {})
        for rule in (pressure, oil):
            self.assertEqual(
                _rule_rows({"calculations": [rule]}, {"codes": [rule["code"]]}, False)[0][1],
                "No leakage at any point",
            )
            self.assertEqual(
                _test_summary(
                    {"calculations": [rule]}, "pressure_vacuum", "Pressure", [rule["code"]], False
                )[3],
                "No leakage at any point",
            )
        before = [
            evaluate_rule(self.rules[code], fields, calcs, {})
            for code in ("REACTANCE_NTBT_NTAT", "REACTANCE_HTBT_HTAT", "REACTANCE_LTBT_LTAT")
        ]
        context = {"prior_results": before}
        self.assertEqual(
            evaluate_rule(self.rules["SC_OVERALL"], fields, calcs, context)["verdict"], "pass"
        )
        next(f for f in fields if f["schema_key"] == "spacers")["status"] = "unreviewed"
        self.assertEqual(
            evaluate_rule(self.rules["SC_OVERALL"], fields, calcs, context)["verdict"], "blocked"
        )
        next(f for f in fields if f["schema_key"] == "lv_observation_BT")["status"] = "unreviewed"
        self.assertEqual(
            evaluate_rule(self.rules["SEPARATE_SOURCE_AC"], fields, calcs, {})["verdict"], "blocked"
        )

    def test_v8_1_reactance_rows_and_summary_use_three_decimals(self):
        fields, calcs = evidence()
        results = [
            evaluate_rule(self.rules[code], fields, calcs, {})
            for code in ("REACTANCE_NTBT_NTAT", "REACTANCE_HTBT_HTAT", "REACTANCE_LTBT_LTAT")
        ]
        data = self.snapshot(fields, results=results)
        rows = _rule_rows(data, {"codes": [rule["code"] for rule in results]}, False)
        self.assertEqual([row[1] for row in rows], ["-0.163 %", "-0.092 %", "-0.204 %"])
        self.assertEqual(rows[-1][3], "1.80 %")
        summary = _test_summary(
            data,
            "short_circuit",
            "Short-circuit withstand",
            [rule["code"] for rule in results],
            False,
        )
        self.assertEqual(summary[3], "-0.204 %")
        self.assertEqual(summary[4], "1.80 %")

    def test_v8_2_ratio_governing_phase_not_mean(self):
        fields, calcs = evidence()
        next(f for f in fields if f["schema_key"] == "ratio.3.AT_A")["value"] = "44.10"
        result = evaluate_rule(self.rules["VOLTAGE_RATIO"], fields, calcs, {})
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["worst_phase"], "AT A")
        self.assertEqual(result["worst_phase_ratio"], "44.10")
        self.assertGreater(float(result["value"]), 0.22)
        self.assertEqual(
            _rule_rows({"calculations": [result]}, {"codes": ["VOLTAGE_RATIO"]}, False)[0][1],
            "AT A 44.10 (+0.22%)",
        )
        self.assertEqual(
            _rule_rows({"calculations": [result]}, {"codes": ["VOLTAGE_RATIO"]}, False)[0][3],
            "0.21 %",
        )
        self.assertEqual(
            _test_summary(
                {"calculations": [result]}, "ratio_vector", "Ratio", ["VOLTAGE_RATIO"], False
            )[3],
            "AT A 44.10 (+0.22%)",
        )

    def test_v8_3_nonprincipal_impedance_and_losses_are_descriptive(self):
        fields, calcs = evidence()
        codes = (
            "IMPEDANCE_HTBT",
            "IMPEDANCE_LTBT",
            "LOSS_100_HTBT",
            "LOSS_100_HTAT",
            "LOSS_100_LTBT",
            "LOSS_100_LTAT",
        )
        results = [evaluate_rule(self.rules[code], fields, calcs, {}) for code in codes]
        self.assertTrue(
            all(rule["verdict"] == "descriptive" and rule["value"] is not None for rule in results)
        )
        rows = _rule_rows({"calculations": results}, {"codes": codes}, False)
        self.assertTrue(all(row[4] == "DESCRIPTIVE" and row[1] != "Not recorded" for row in rows))
        limits = {rule["code"]: row[2] for rule, row in zip(results, rows)}
        self.assertEqual(limits["LOSS_100_HTBT"], "Principal-tap EEL limit only")
        self.assertEqual(limits["LOSS_100_LTAT"], "Principal-tap EEL limit only")
        self.assertEqual(limits["IMPEDANCE_HTBT"], "No tap-specific declaration")
        after = evaluate_rule(self.rules["LOSS_100_NTAT"], fields, calcs, {})
        self.assertEqual(
            _rule_rows({"calculations": [after]}, {"codes": ["LOSS_100_NTAT"]}, False)[0][2],
            "BT/AT comparison only",
        )

    def test_v8_4_observation_summary_margin_is_dash(self):
        rule = {
            "code": "PRESSURE_TEST",
            "title": "Pressure",
            "version": 2,
            "v2_status": "OBSERVATION",
            "verdict": "pass",
            "value": "No leakage at any point",
            "unit": "",
            "source_clause": "clause TBC",
            "parameters": {"v2_kind": "pressure_observation"},
        }
        summary = _test_summary(
            {"calculations": [rule]}, "pressure_vacuum", "Pressure", ["PRESSURE_TEST"], False
        )
        self.assertEqual(summary[4], "—")

    def test_v8_5_confirmed_review_import_skips_two_undecided_cells(self):
        path = Path(__file__).resolve().parents[2] / "benchmark" / "import_pending_37_review.py"
        spec = spec_from_file_location("import_pending_37_review", path)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        rows = [(None, f"Field {i}", f"Checked {i}") for i in range(35)]
        rows.extend(
            [
                (None, "Witness Name", "(your decision)"),
                (None, "LV withstand observation", "(your decision)"),
            ]
        )
        self.assertEqual(len(module.MAPPING), 37)
        self.assertEqual(module.selected_indexes(rows, include_flagged=True), list(range(35)))
        self.assertEqual(
            module.selected_indexes(rows, include_flagged=False),
            [i for i in range(35) if i not in module.FLAGGED],
        )

    def test_rev9_sc_overall_uses_checked_observations_and_reactance(self):
        fields, calcs = evidence()
        fields = [
            field
            for field in fields
            if field["schema_key"]
            not in ("overall_engineer_verdict", "no_visible_defects_confirmed")
        ]
        next(field for field in fields if field["schema_key"] == "oil")["value"] = "Clear"
        prior = [
            evaluate_rule(self.rules[code], fields, calcs, {})
            for code in ("REACTANCE_NTBT_NTAT", "REACTANCE_HTBT_HTAT", "REACTANCE_LTBT_LTAT")
        ]
        result = evaluate_rule(self.rules["SC_OVERALL"], fields, calcs, {"prior_results": prior})
        self.assertEqual((result["verdict"], result["v2_status"]), ("pass", "OBSERVATION"))
        self.assertIn(
            "No abnormalities",
            _rule_rows({"calculations": [result]}, {"codes": ["SC_OVERALL"]}, False)[0][1],
        )
        next(field for field in fields if field["schema_key"] == "spacers")["status"] = "unreviewed"
        self.assertEqual(
            evaluate_rule(self.rules["SC_OVERALL"], fields, calcs, {"prior_results": prior})[
                "verdict"
            ],
            "blocked",
        )
        prior[0]["verdict"] = "fail"
        self.assertEqual(
            evaluate_rule(self.rules["SC_OVERALL"], fields, calcs, {"prior_results": prior})[
                "verdict"
            ],
            "blocked",
        )

    def test_rev9_annex_a_keeps_each_checked_source_date(self):
        fields = [
            {
                "form_type": "routine_test",
                "schema_key": "date_BT",
                "value": "09-11-2025",
                "status": "verified",
                "document_id": "routine",
            },
            {
                "form_type": "routine_test",
                "schema_key": "date_AT",
                "value": "10-11-2025",
                "status": "verified",
                "document_id": "routine",
            },
            {
                "form_type": "temperature_rise",
                "schema_key": "test_dates",
                "value": "09-11-2025 and 10-11-2025",
                "status": "verified",
                "document_id": "temperature",
            },
        ]
        data = self.snapshot(fields)
        data["documents"] = [
            {"id": "routine", "name": "Routine.pdf"},
            {"id": "temperature", "name": "Temperature.pdf"},
        ]
        rows = _system(data, "source_records", SimpleNamespace(), False)
        self.assertEqual(rows[0][2], "BT: 09-11-2025; AT: 10-11-2025")
        self.assertEqual(rows[1][2], "Test dates: 09-11-2025 and 10-11-2025")

    def test_rev9_annex_c_names_ai_prefill_and_human_confirmation(self):
        data = self.snapshot()
        data["source_review_provenance"] = (
            "35 source rows pre-filled by AI assistant; confirmed by sampreeth."
        )
        rows = _system(data, "review_summary", SimpleNamespace(), False)
        self.assertIn("pre-filled by AI assistant; confirmed by sampreeth", str(rows))
