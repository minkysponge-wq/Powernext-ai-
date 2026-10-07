import json
from pathlib import Path

from django.test import SimpleTestCase

from .reviewed_calculations import worksheet_profile_value
from .transformer import calculate_datasheet, load_loss_at_75, resistance_at_75, total_loss


class TransformerTests(SimpleTestCase):
    def test_printed_profile_labels_are_interpreted_without_changing_source_text(self):
        self.assertEqual(worksheet_profile_value("material", "Cu wound"), "Cu")
        self.assertEqual(worksheet_profile_value("phases", "3ph"), "3")
        self.assertEqual(worksheet_profile_value("efficiency_level", "EEL-1"), "1")
        self.assertEqual(worksheet_profile_value("phases", "2ph"), "2ph")

    def test_six_datasheet_rows_from_measured_inputs(self):
        fixture = json.loads(
            Path(__file__)
            .with_name("fixtures")
            .joinpath("synthetic_datasheet_v217.json")
            .read_text()
        )
        values = fixture["values"]
        rows = calculate_datasheet(values)
        # Printed-input reproduction tolerance, not a claim of exact legacy arithmetic.
        tolerances = {
            "Rhv": 0.0006,
            "Rlv": 0.0006,
            "load_loss_100": 0.2,
            "stray_loss": 0.06,
            "Z75": 0.001,
            "X50": 0.001,
            "total_loss_100": 0.2,
        }
        for i, row in enumerate(rows):
            for key, tolerance in tolerances.items():
                with self.subTest(stage=row["stage"], quantity=key):
                    self.assertAlmostEqual(
                        row[key], float(values[f"calculated.{i}.{key}"]), delta=tolerance
                    )
            self.assertEqual(row["verdict_100"], "Within limit")
            if i < 2:
                self.assertAlmostEqual(
                    row["total_loss_50"], float(values[f"calculated.{i}.total_loss_50"]), delta=0.05
                )
                self.assertEqual(row["verdict_50"], "Within limit")

    def test_copper_resistance_and_opposite_stray_correction(self):
        self.assertAlmostEqual(resistance_at_75(1, 75), 1)
        self.assertAlmostEqual(resistance_at_75(2, 25), 2 * 310 / 260)
        corrected, stray = load_loss_at_75(1100, 1000, 25)
        self.assertAlmostEqual(stray, 100 * 260 / 310)
        self.assertAlmostEqual(corrected, 1000 * 310 / 260 + stray)
        self.assertAlmostEqual(total_loss(400, 2000, 0.5), 900)

    def test_invalid_engineering_inputs_rejected(self):
        for args in [(100, 110, 25), (-1, 0, 25), (100, 50, -235)]:
            with self.assertRaises(ValueError):
                load_loss_at_75(*args)
        with self.assertRaises(ValueError):
            resistance_at_75(0, 25)
        with self.assertRaises(ValueError):
            total_loss(1, 2, -0.5)

    def test_limits_are_input_not_forced_pass(self):
        values = json.loads(
            Path(__file__)
            .with_name("fixtures")
            .joinpath("synthetic_datasheet_v217.json")
            .read_text()
        )["values"]
        values["guarantee_100"] = "1000"
        values["guarantee_50"] = "500"
        rows = calculate_datasheet(values)
        self.assertEqual(rows[0]["verdict_100"], "Exceeds limit")
        self.assertEqual(rows[0]["verdict_50"], "Exceeds limit")
