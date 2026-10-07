import copy
from types import SimpleNamespace

from django.test import SimpleTestCase

from .quality import (
    assess,
    canonical_unit,
    requires_individual_review,
    review_priority,
    validate_policy,
    validation_policy,
)


class ValidationPolicyTests(SimpleTestCase):
    def field(self, pk, key, value, unit="", source="sheet-a", form="fixture"):
        return SimpleNamespace(
            pk=pk,
            key=key,
            value=value,
            unit=unit,
            status="unreviewed",
            page=1,
            document=None,
            document_id=None,
            origin="digital",
            context={
                "schema_key": key,
                "schema_page": 1,
                "form_type": form,
                "source_reference": source,
            },
        )

    def policy(self, checks):
        policy = copy.deepcopy(validation_policy())
        policy["checks"] = checks
        return policy

    def test_format_and_range_flag_but_never_automatically_approve(self):
        policy = self.policy(
            [
                {"type": "format", "field": "serial_number", "format": "identifier"},
                {"type": "range", "field": "frequency", "unit": "Hz", "min": 45, "max": 65},
            ]
        )
        good = [self.field(1, "serial_number", "SN/012"), self.field(2, "frequency", "50", "Hz")]
        checks = assess(good, policy)
        self.assertTrue(all(c["state"] == "needs_review" for c in checks.values()))
        bad = [self.field(3, "serial_number", "invalid!?"), self.field(4, "frequency", "75", "Hz")]
        checks = assess(bad, policy)
        self.assertTrue(all(c["failed"] for c in checks.values()))

    def test_new_arithmetic_rule_is_config_only_and_requires_units(self):
        policy = self.policy(
            [{"type": "sum", "table": "totals", "of": ["A", "B"], "equals": "Total"}]
        )
        fields = [
            self.field(i, "totals.0." + key, value, "W")
            for i, (key, value) in enumerate([("A", "10.0"), ("B", "20.0"), ("Total", "30.0")], 1)
        ]
        checks = assess(fields, policy)
        self.assertTrue(all(c["state"] == "automatically_checked" for c in checks.values()))
        self.assertTrue(all(c["passed"] for c in checks.values()))
        fields[1].unit = ""
        self.assertTrue(all(c["state"] == "needs_review" for c in assess(fields, policy).values()))

    def test_required_ratio_trend_and_limits_are_review_signals(self):
        policy = self.policy(
            [
                {"type": "required", "field": "sample_code"},
                {
                    "type": "ratio",
                    "table": "impedance",
                    "of": ["A", "B"],
                    "units": ["V", "V"],
                    "expect": 2,
                    "tol": 0.1,
                },
                {
                    "type": "monotonic",
                    "table": "heat",
                    "column": "temperature",
                    "unit": "C",
                    "direction": "increasing",
                },
                {
                    "type": "limit",
                    "field": "loss",
                    "unit": "W",
                    "max": 980,
                    "near_margin": 20,
                    "source_clause": "Confirmed fixture limit",
                },
            ]
        )
        fields = [
            self.field(1, "sample_code", ""),
            self.field(2, "impedance.0.A", "20", "V"),
            self.field(3, "impedance.0.B", "10", "V"),
            self.field(4, "heat.0.temperature", "30", "C"),
            self.field(5, "heat.1.temperature", "32", "C"),
            self.field(6, "loss", "970", "W"),
        ]
        checks = assess(fields, policy)
        self.assertIn("Required field is blank.", checks[1]["issues"])
        self.assertTrue(checks[2]["passed"] and checks[4]["passed"])
        self.assertTrue(any("close to a boundary" in x for x in checks[6]["issues"]))
        self.assertTrue(all(c["state"] == "needs_review" for c in checks.values()))

    def test_invalid_or_unsafe_rule_is_rejected(self):
        for rule in [
            {"type": "python", "expression": "1+1"},
            {"type": "range", "field": "x", "min": 2, "max": 1},
            {"type": "sum", "of": ["a", "b"], "equals": "c", "tol": -1},
            {"type": "limit", "field": "x", "max": 1},
            {"type": "ratio", "of": ["a", "b"], "units": ["V", "V"], "expect": 1, "tol": -1},
        ]:
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                validate_policy(self.policy([rule]))

    def test_priority_is_only_a_review_order_not_verification(self):
        fields = [
            self.field(1, "sample_code", "ABC123"),
            self.field(2, "ordinary_note", "Visible note"),
        ]
        checks = assess(fields, self.policy([]))
        self.assertTrue(review_priority(fields[0], checks[1]))
        self.assertFalse(review_priority(fields[1], checks[2]))
        self.assertEqual([checks[f.pk]["state"] for f in fields], ["needs_review", "needs_review"])
        fields[1].context["extraction_issue"] = "invalid_source_box"
        self.assertTrue(review_priority(fields[1], checks[2]))

    def test_matching_critical_customer_and_rating_still_need_individual_review(self):
        fields = [
            self.field(1, "customer", "Fictional Transformer Co.", source="request"),
            self.field(2, "customer", "Fictional Transformer Co.", source="logsheet"),
            self.field(3, "rated_power", "250", unit="kVA", source="request"),
            self.field(4, "rated_power", "250", unit="kVA", source="logsheet"),
        ]
        checks = assess(fields)
        self.assertTrue(all(checks[f.pk]["state"] == "needs_review" for f in fields))
        self.assertTrue(all(review_priority(f, checks[f.pk]) for f in fields))

    def test_arithmetic_agreement_does_not_clear_critical_current_readings(self):
        fields = [
            self.field(i, "shots.0." + key, value, "kA")
            for i, (key, value) in enumerate(
                [("rms_U", "2.0"), ("rms_V", "2.1"), ("rms_W", "2.2"), ("rms_avg", "2.1")], 1
            )
        ]
        for field in fields:
            field.origin = "scan"
        checks = assess(fields)
        self.assertTrue(all(requires_individual_review(f) for f in fields))
        self.assertTrue(all(checks[f.pk]["state"] == "needs_review" for f in fields))
        self.assertTrue(
            all(
                any("individual review required" in issue for issue in checks[f.pk]["issues"])
                for f in fields
            )
        )

    def test_unit_whitelist_normalizes_spelling_but_preserves_raw_value(self):
        policy = self.policy([{"type": "unit", "field": "duration", "allowed": ["s"]}])
        good = self.field(1, "duration", "60", "Seconds")
        missing = self.field(2, "duration", "60", "")
        wrong = self.field(3, "duration", "60", "ms")
        checks = assess([good, missing, wrong], policy)
        self.assertEqual(canonical_unit(good.unit), "s")
        self.assertEqual(good.unit, "Seconds")
        self.assertTrue(any("Unit spelling is allowed" in item for item in checks[1]["passed"]))
        self.assertTrue(any("Unit is missing" in item for item in checks[2]["failed"]))
        self.assertTrue(any("Unit is missing" in item for item in checks[3]["failed"]))

    def test_unit_rule_rejects_empty_configuration(self):
        with self.assertRaises(ValueError):
            validate_policy(self.policy([{"type": "unit", "field": "frequency", "allowed": []}]))

    def test_choice_labels_and_signature_presence_flag_ambiguous_reads(self):
        policy = self.policy(
            [
                {"type": "format", "field": "method", "format": "choice_label"},
                {"type": "format", "field": "signature_present", "format": "boolean_presence"},
            ]
        )
        fields = [
            self.field(1, "method", "✓"),
            self.field(2, "method", "ONAN"),
            self.field(3, "signature_present", "John?"),
            self.field(4, "signature_present", "Yes"),
        ]
        checks = assess(fields, policy)
        self.assertTrue(checks[1]["failed"])
        self.assertFalse(checks[2]["failed"])
        self.assertTrue(checks[3]["failed"])
        self.assertFalse(checks[4]["failed"])
        self.assertEqual(fields[0].value, "✓")
