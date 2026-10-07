"""Shared reviewed-input gate for screens and report snapshots."""

import re

from .transformer import calculate_datasheet


def worksheet_profile_value(key, value):
    """Interpret explicit profile labels while preserving reviewed source text."""
    text = str(value).strip()
    aliases = {
        "material": {"cu wound": "Cu", "cu": "Cu", "copper": "Cu"},
        "phases": {"3ph": "3", "3 phase": "3", "3": "3"},
        "efficiency_level": {"eel-1": "1", "eel 1": "1", "1": "1"},
    }
    return aliases.get(key, {}).get(text.casefold(), value)


def transformer_rows(doc):
    return transformer_rows_for_fields(doc.field_set.all())


def transformer_rows_for_fields(source_fields):
    fields = {
        f.context.get("schema_key") or re.split(r"\.p\d+\.", f.key, maxsplit=1)[-1]: f
        for f in source_fields
    }

    class ReviewedValues(dict):
        def __getitem__(self, key):
            field = fields.get(key)
            if field is None or field.status != "verified":
                raise ValueError("Review and verify " + key + " before calculating.")
            import re

            expected = None
            if key == "rated_power":
                expected = "kVA"
            elif key in ("rated_hv", "rated_lv"):
                expected = "V"
            elif key.startswith("guarantee_"):
                expected = "W"
            elif key.startswith("hv_resistance.") and ".R" in key:
                expected = "Ohm"
            elif key.startswith("lv_resistance.") and ".R" in key:
                expected = "mOhm"
            elif key.endswith(".temperature"):
                expected = "degC"
            elif re.search(r"\.V[123]?$", key):
                expected = "V"
            elif re.search(r"\.I[123]?$", key):
                expected = "A"
            elif re.search(r"\.P[123]?$", key):
                expected = "W"
            elif key.endswith(".f"):
                expected = "Hz"
            aliases = {"Ω": "Ohm", "ohm": "Ohm", "mΩ": "mOhm", "°C": "degC", "C": "degC"}
            actual = aliases.get(field.unit, field.unit)
            if expected and actual != expected:
                raise ValueError(f"Confirm unit {expected} for {key} before calculating.")
            return worksheet_profile_value(key, field.value)

    return calculate_datasheet(ReviewedValues())
