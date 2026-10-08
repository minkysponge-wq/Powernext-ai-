"""One executable case per v2 rule, plus boundary and blocked-path cases."""

from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from .calculations import evaluate_rule
from .management.commands.seed_verdict_candidates import candidates
from .management.commands.seed_verdict_v2 import v2_definitions
from .models import Rule


def rule_library():
    old = [
        SimpleNamespace(code=code, title=title, operation=operation, parameters=params)
        for code, title, operation, params in candidates()
    ]
    return {item["code"]: SimpleNamespace(**item, version=2) for item in v2_definitions(old)}


def evidence():
    fields = []

    def add(form, key, value, unit=""):
        fields.append(
            {
                "key": form + "." + key,
                "form_type": form,
                "schema_key": key,
                "value": str(value),
                "unit": unit,
                "status": "verified",
            }
        )

    add("loss_calculation", "rated_power", 250, "kVA")
    add("loss_calculation", "efficiency_level", "EEL-1")
    add("transformer_proforma", "impedance_at_75", 4.5, "%")
    add("transformer_proforma", "rated_hv", 11000, "V")
    add("transformer_proforma", "rated_lv", 433, "V")
    add("transformer_proforma", "vector_group", "Dyn11")
    add("transformer_proforma", "tap_range", "+5% to -10%")
    add("transformer_proforma", "tap_step", "2.5%")
    add("transformer_proforma", "circular", "Ticked")
    add("transformer_proforma", "guaranteed_temp_rise_1", 35, "K")
    add("transformer_proforma", "guaranteed_temp_rise_2", 40, "K")
    add("routine_test", "ratio.3.tap", "3")
    for phase in "ABC":
        add("routine_test", "ratio.3.BT_" + phase, "44.00")
    for phase in "ABC":
        add("routine_test", "ratio.3.AT_" + phase, "44.01")
    for phase in (1, 2, 3):
        add("loss_measurement", f"hv_resistance.1.AT_{phase}", 4, "Ω")
        add("loss_measurement", f"lv_resistance.0.AT_{phase}", 4, "mΩ")
    add("loss_measurement", "hv_resistance.1.tap", "N")
    for key in ("ir_hv_earth_AT", "ir_lv_earth_AT", "ir_hv_lv_AT"):
        add("routine_test", key, 1, "GΩ")
    add("temperature_rise", "top_oil_rise", 26.1, "K")
    add("temperature_rise", "hv_winding_rise", 39.7, "K")
    add("temperature_rise", "lv_winding_rise", 34.3, "K")
    add("loss_measurement", "no_load_100_percent", 0.51, "%")
    add("loss_measurement", "no_load_112_percent", 1.28, "%")
    for key in (
        "induced_observation_BT",
        "induced_observation_AT",
        "hv_observation_BT",
        "hv_observation_AT",
        "lv_observation_BT",
        "lv_observation_AT",
    ):
        add("routine_test", key, "No disruptive discharge, withstood")
    add("pressure_oil_leakage", "routine_pressure_observation", "No leakage observed")
    add("pressure_oil_leakage", "routine_pressure", "91kpa")
    add("pressure_oil_leakage", "oil_observation", "No leakage observed")
    add("short_circuit", "no_visible_defects_confirmed", "confirmed")
    add("short_circuit", "overall_engineer_verdict", "confirmed")
    add("short_circuit", "conductor_core_clamps", "No visible damage")
    add("short_circuit", "spacers", "Intact")
    add("short_circuit", "oil", "Clean")
    add("short_circuit", "during_test", "No abnormalities")
    add("short_circuit", "after_test", "No abnormalities")
    x = {
        "NTBT": 4.299311815780109,
        "NTAT": 4.305536983937887,
        "HTBT": 4.365014858647969,
        "HTAT": 4.36932491950878,
        "LTBT": 4.396990084735396,
        "LTAT": 4.406415419512878,
    }
    rows = []
    for stage in x:
        rows.append(
            {
                "stage": stage,
                "X50": x[stage],
                "Z75": 4.38,
                "total_loss_50": 908 if stage.endswith("BT") else 907,
                "total_loss_100": 2486 if stage.endswith("BT") else 2484,
                "limit_50": 980,
                "limit_100": 2930,
            }
        )
    calculations = [
        {"form_type": "loss_calculation", "source": "Reviewed worksheet", "rows": rows, "error": ""}
    ]
    return fields, calculations


class RuleV2Tests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.library = rule_library()

    def run_rule(self, code, fields=None, calculations=None, context=None):
        if fields is None or calculations is None:
            fields, calculations = evidence()
        base = {"requested_tests_text": "Short-circuit withstand; no-load current at 112.5%"}
        base.update(context or {})
        if code == "SC_OVERALL" and "prior_results" not in base:
            base["prior_results"] = [
                self.run_rule(name, fields, calculations)
                for name in ("REACTANCE_NTBT_NTAT", "REACTANCE_HTBT_HTAT", "REACTANCE_LTBT_LTAT")
            ]
        return evaluate_rule(self.library[code], fields, calculations, base)

    def test_v1_definition_count_and_v2_status(self):
        self.assertEqual(len(self.library), 28)
        self.assertTrue(
            all(
                item.parameters["decision_status"] == "reviewed by team, pending CPRI adoption"
                for item in self.library.values()
            )
        )

    def test_pending_cpri_adoption_cannot_be_marked_confirmed(self):
        item = self.library["LOSS_50_NTBT"]
        record = Rule(
            code=item.code,
            version=2,
            title=item.title,
            operation=item.operation,
            parameters=item.parameters,
            source_clause=item.source_clause,
            status="confirmed",
        )
        with self.assertRaises(ValidationError):
            record.clean()

    def test_limit_mismatch_blocks(self):
        fields, calcs = evidence()
        calcs[0]["rows"][0]["limit_50"] = 981
        self.assertIn("limit mismatch", self.run_rule("LOSS_50_NTBT", fields, calcs)["message"])

    def test_reactance_signed_pinned_values(self):
        for code, value in [
            ("REACTANCE_NTBT_NTAT", "-0.163"),
            ("REACTANCE_HTBT_HTAT", "-0.092"),
            ("REACTANCE_LTBT_LTAT", "-0.205"),
        ]:
            self.assertEqual(self.run_rule(code)["value"], value)

    def test_non_circular_reactance_blocks(self):
        fields, calcs = evidence()
        next(f for f in fields if f["schema_key"] == "circular")["value"] = "Not ticked"
        result = self.run_rule("REACTANCE_NTBT_NTAT", fields, calcs)
        self.assertEqual(result["verdict"], "blocked")
        self.assertIn("non-circular limit not confirmed", result["message"])

    def test_112_requires_crf_scope(self):
        self.assertEqual(
            self.run_rule(
                "NO_LOAD_CURRENT_112", context={"requested_tests_text": "Short-circuit only"}
            )["verdict"],
            "not_applicable",
        )
        self.assertEqual(
            self.run_rule("NO_LOAD_CURRENT_112", context={"requested_tests_text": ""})["verdict"],
            "blocked",
        )

    def test_unknown_observation_blocks(self):
        fields, calcs = evidence()
        next(f for f in fields if f["schema_key"] == "induced_observation_AT")[
            "value"
        ] = "Possibly fine"
        self.assertEqual(self.run_rule("INDUCED_DIELECTRIC", fields, calcs)["verdict"], "blocked")

    def test_temperature_uses_stricter_guarantee(self):
        result = self.run_rule("HV_WINDING_RISE")
        self.assertEqual(result["limit"], "40")
        self.assertIn(
            "stricter than cited IS 1180 (Part 1):2014 incl. Amendment 4 limit 45 K",
            result["limit_provenance"],
        )
        self.assertEqual(result["quality_flag"], "near_limit")

    def test_pressure_unit_normalisation(self):
        self.assertEqual(self.run_rule("PRESSURE_TEST")["normalised_pressure"], "91 kPa")

    def test_sc_overall_requires_checked_during_and_after_observations(self):
        fields, calcs = evidence()
        next(f for f in fields if f["schema_key"] == "after_test")["value"] = "Unexpected movement"
        self.assertEqual(self.run_rule("SC_OVERALL", fields, calcs)["verdict"], "blocked")


EXPECTED = {
    "LOSS_50_NTBT": ("ACTIVE", "pass"),
    "LOSS_100_NTBT": ("ACTIVE", "pass"),
    "IMPEDANCE_NTBT": ("ACTIVE", "pass"),
    "LOSS_50_NTAT": ("DESCRIPTIVE", "descriptive"),
    "LOSS_100_NTAT": ("DESCRIPTIVE", "descriptive"),
    "IMPEDANCE_NTAT": ("DESCRIPTIVE", "descriptive"),
    "LOSS_100_HTBT": ("DESCRIPTIVE", "descriptive"),
    "LOSS_100_HTAT": ("DESCRIPTIVE", "descriptive"),
    "LOSS_100_LTBT": ("DESCRIPTIVE", "descriptive"),
    "LOSS_100_LTAT": ("DESCRIPTIVE", "descriptive"),
    "IMPEDANCE_HTBT": ("DESCRIPTIVE", "descriptive"),
    "IMPEDANCE_LTBT": ("DESCRIPTIVE", "descriptive"),
    "IMPEDANCE_HTAT": ("DESCRIPTIVE", "descriptive"),
    "IMPEDANCE_LTAT": ("DESCRIPTIVE", "descriptive"),
    "REACTANCE_NTBT_NTAT": ("ACTIVE", "pass"),
    "REACTANCE_HTBT_HTAT": ("ACTIVE", "pass"),
    "REACTANCE_LTBT_LTAT": ("ACTIVE", "pass"),
    "NO_LOAD_CURRENT_100": ("ACTIVE", "pass"),
    "NO_LOAD_CURRENT_112": ("ACTIVE", "pass"),
    "TOP_OIL_RISE": ("ACTIVE", "pass"),
    "HV_WINDING_RISE": ("ACTIVE", "marginal"),
    "LV_WINDING_RISE": ("ACTIVE", "pass"),
    "INDUCED_DIELECTRIC": ("OBSERVATION", "pass"),
    "VOLTAGE_RATIO": ("ACTIVE", "pass"),
    "SEPARATE_SOURCE_AC": ("OBSERVATION", "pass"),
    "PRESSURE_TEST": ("OBSERVATION", "pass"),
    "OIL_LEAKAGE": ("OBSERVATION", "pass"),
    "SC_OVERALL": ("OBSERVATION", "pass"),
}


def make_rule_test(code, expected):
    def test(self):
        result = self.run_rule(code)
        self.assertEqual(result["v2_status"], expected[0])
        self.assertEqual(result["verdict"], expected[1], result["message"])
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["provenance_status"], "reviewed by team, pending CPRI adoption")

    test.__name__ = "test_rule_" + code.lower()
    return test


for _code, _expected in EXPECTED.items():
    setattr(RuleV2Tests, "test_rule_" + _code.lower(), make_rule_test(_code, _expected))
