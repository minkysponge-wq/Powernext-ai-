from types import SimpleNamespace

from django.test import SimpleTestCase

from .calculations import evaluate_rule


class CalculationTests(SimpleTestCase):
    def rule(self, **changes):
        return SimpleNamespace(
            **dict(
                dict(
                    code="DEMO",
                    version=1,
                    title="Fixture only",
                    operation="difference",
                    parameters={"fields": ["hot", "ambient"], "unit": "C", "upper": "50"},
                    source_clause="Test fixture; not a laboratory standard",
                    status="assumed",
                ),
                **changes,
            )
        )

    def fields(self):
        return [
            {"key": "hot", "value": "80.1", "unit": "C", "status": "verified"},
            {"key": "ambient", "value": "30.1", "unit": "C", "status": "verified"},
        ]

    def test_inclusive_boundary_decimal_exact(self):
        result = evaluate_rule(self.rule(), self.fields())
        self.assertEqual(result["value"], "50.0")
        self.assertEqual(result["state"], "within_limits")
        self.assertIn("PROVISIONAL", result["message"])

    def test_missing_unreviewed_unit_and_nonfinite_blocked(self):
        for change in [
            {"status": "unreviewed"},
            {"value": "NaN"},
            {"value": "80 or 90"},
            {"unit": "K"},
        ]:
            fields = self.fields()
            fields[0].update(change)
            self.assertEqual(evaluate_rule(self.rule(), fields)["state"], "blocked")
        self.assertEqual(evaluate_rule(self.rule(), [])["state"], "blocked")

    def test_outlier_is_not_corrected(self):
        fields = self.fields()
        fields[0]["value"] = "500"
        result = evaluate_rule(self.rule(), fields)
        self.assertEqual(result["state"], "outside_limits")
        self.assertEqual(fields[0]["value"], "500")

    def test_bad_rules_are_blocked(self):
        for rule in [
            self.rule(status="confirmed", source_clause=""),
            self.rule(operation="eval"),
            self.rule(
                parameters={"fields": ["hot", "ambient"], "unit": "C", "lower": 60, "upper": 50}
            ),
        ]:
            self.assertEqual(evaluate_rule(rule, self.fields())["state"], "blocked")
