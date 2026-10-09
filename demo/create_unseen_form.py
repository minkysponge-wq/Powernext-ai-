"""Generate a second, entirely fictional same-schema form and CSV export."""

import csv
import os
import sys
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from lab.extraction import schemas
from lab.fixed_template import TESTS, default_fixed_definition, report_field_specs
from lab.quality import canonical_unit, validation_policy
from lab.synthetic_demo import STATION_POLICIES, WORKSHEET, mapped_keys, reading

DESTINATION = ROOT / "demo" / "forms"
SAMPLE = "SYN-UNSEEN-002"
SERIES = "SYN-SERIES-002"
CUSTOMER = "SYNTHETIC SECOND FORM - Example Grid Labs"
REQUEST = {
    "customer": CUSTOMER,
    "customer_address": "Fictional site, Mysuru",
    "manufacturer": "SYNTHETIC DEMO Manufacturer B",
    "sample_particulars": "Fictional 250 kVA 11 kV/433 V Dyn11 transformer - second unit",
    "requested_tests": [policy["form_type"] for policy in STATION_POLICIES],
}
JOB = SimpleNamespace(request_snapshot=REQUEST, sample_code=SAMPLE, test_series=SERIES)
OVERRIDES = {
    ("routine_test", "ir_hv_earth_BT"): "1.27",
    ("routine_test", "ir_hv_earth_AT"): "1.25",
    ("routine_test", "ir_lv_earth_BT"): "1.33",
    ("routine_test", "ratio.3.BT_A"): "44.02",
    ("routine_test", "ratio.3.AT_A"): "44.10",
    ("loss_measurement", "no_load_112_percent"): "1.31",
    ("temperature_rise", "hv_winding_rise"): "39.4",
}


def rows():
    needed = mapped_keys(default_fixed_definition())
    unit_checks = [
        rule for rule in validation_policy()["checks"] if rule["type"] in ("unit", "range")
    ]
    mapped_units = {
        (spec.get("form_type", default_form), spec["key"].replace("{principal}", "3")): spec["unit"]
        for test_id, _, default_form, _, _ in TESTS
        for spec in report_field_specs(test_id)
        if spec.get("unit")
    }
    needed.update({("transformer_proforma", "tap_range"), ("transformer_proforma", "tap_step")})
    needed.update(
        ("routine_test", f"ratio.{index}.{suffix}")
        for index in range(7)
        for suffix in ("tap", "BT_A", "BT_B", "BT_C", "AT_A", "AT_B", "AT_C")
    )
    needed.add(("transformer_proforma", "circular"))
    needed.update(
        {("loss_measurement", "no_load_100_limit_percent"), ("loss_measurement", "no_load_112_limit_percent")}
    )
    for policy in STATION_POLICIES:
        needed.update((policy["form_type"], key) for key in policy["lock_required"])
        needed.add((policy["form_type"], policy["result_any_of"][0]))
    for schema in schemas():
        form_type = schema["form_type"]
        for spec in schema["fields"]:
            key = spec["key"]
            active = (form_type, key) in needed or (
                form_type == "loss_calculation" and key in WORKSHEET
            )
            value, unit = reading(JOB, form_type, key) if active else ("", "")
            value = OVERRIDES.get((form_type, key), value)
            if value and key in ("serial", "serial_number"):
                value = "SYN-2042"
            if value and not unit and (form_type, key) in mapped_units:
                unit = mapped_units[(form_type, key)]
            if value:
                for rule in unit_checks:
                    if rule.get("form_type", form_type) != form_type or not fnmatchcase(key, rule["field"]):
                        continue
                    if rule["type"] == "unit":
                        if canonical_unit(unit) not in {canonical_unit(item) for item in rule["allowed"]}:
                            unit = rule["allowed"][0]
                    elif rule.get("unit") and canonical_unit(unit) != canonical_unit(rule["unit"]):
                        unit = rule["unit"]
            yield form_type, spec["page"], key, value, unit, spec["label"]


def write_csv(data):
    path = DESTINATION / f"{SAMPLE}-readings.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["form_type", "page", "key", "value", "unit"])
        writer.writerows(row[:5] for row in data)
    return path


def write_form(data):
    path = DESTINATION / f"{SAMPLE}-routine.pdf"
    font_path = ROOT / "app" / "lab" / "fonts" / "DejaVuSans.ttf"
    pdfmetrics.registerFont(TTFont("UnseenDejaVu", str(font_path)))
    routine = {(form_type, key): (value, unit, label) for form_type, _, key, value, unit, label in data}
    keys = [
        "sample_code", "series", "ir_hv_earth_BT", "ir_hv_earth_AT",
        "ir_lv_earth_BT", "ir_lv_earth_AT", "ir_hv_lv_BT", "ir_hv_lv_AT",
        "ratio.3.tap", "ratio.3.BT_A", "ratio.3.BT_B", "ratio.3.BT_C",
        "ratio.3.AT_A", "ratio.3.AT_B", "ratio.3.AT_C",
        "hv_voltage_BT", "hv_voltage_AT", "induced_voltage_BT",
        "induced_voltage_AT", "hv_observation_BT", "hv_observation_AT",
    ]
    labels = {
        "sample_code": "Sample code", "series": "Test series",
        "ir_hv_earth_BT": "HV-earth insulation resistance - before",
        "ir_hv_earth_AT": "HV-earth insulation resistance - after",
        "ir_lv_earth_BT": "LV-earth insulation resistance - before",
        "ir_lv_earth_AT": "LV-earth insulation resistance - after",
        "ir_hv_lv_BT": "HV-LV insulation resistance - before",
        "ir_hv_lv_AT": "HV-LV insulation resistance - after",
        "ratio.3.tap": "Principal tap label",
        "hv_voltage_BT": "Applied HV voltage - before",
        "hv_voltage_AT": "Applied HV voltage - after",
        "induced_voltage_BT": "Induced voltage - before",
        "induced_voltage_AT": "Induced voltage - after",
        "hv_observation_BT": "Applied HV observation - before",
        "hv_observation_AT": "Applied HV observation - after",
    }
    for stage, word in (("BT", "before"), ("AT", "after")):
        for phase in "ABC":
            labels[f"ratio.3.{stage}_{phase}"] = f"Principal tap ratio - {word}, phase {phase}"
    page = canvas.Canvas(str(path), pagesize=(595, 842))
    page.setTitle(f"{SAMPLE} - fictional routine logsheet")
    page.setFillColor(colors.HexColor("#032D57"))
    page.rect(0, 780, 595, 62, stroke=0, fill=1)
    page.setFillColor(colors.white)
    page.setFont("UnseenDejaVu", 17)
    page.drawString(42, 806, "VectorLab - second synthetic logsheet")
    page.setFillColor(colors.HexColor("#283849"))
    page.setFont("UnseenDejaVu", 8.5)
    page.drawString(42, 758, "SYNTHETIC TRAINING FORM - not laboratory evidence")
    page.drawString(42, 741, f"Customer: {CUSTOMER}")
    page.drawString(42, 724, f"Sample: {SAMPLE}     Series: {SERIES}     Source page: 1")
    page.setStrokeColor(colors.HexColor("#C8D3DD"))
    page.line(42, 708, 553, 708)
    page.setFont("UnseenDejaVu", 8.5)
    page.setFillColor(colors.HexColor("#032D57"))
    page.drawString(48, 690, "Measurement")
    page.drawString(340, 690, "Value")
    page.drawString(515, 690, "Unit")
    for index, key in enumerate(keys):
        value, unit, _ = routine.get(("routine_test", key), ("", "", key))
        y = 669 - index * 26
        if index % 2 == 0:
            page.setFillColor(colors.HexColor("#F3F7FA"))
            page.rect(42, y - 7, 511, 25, fill=1, stroke=0)
        page.setFillColor(colors.HexColor("#283849"))
        page.drawString(48, y, labels[key])
        if "observation" in key:
            page.setFont("UnseenDejaVu", 7.2)
        page.drawString(340, y, value or "[blank]")
        if "observation" in key:
            page.setFont("UnseenDejaVu", 8.5)
        page.drawString(515, y, unit)
    page.setFillColor(colors.HexColor("#596D7C"))
    page.setFont("UnseenDejaVu", 7.5)
    page.drawString(42, 78, "The CSV export contains all schema fields, including this page. Values require human review in VectorLab.")
    page.drawString(42, 62, "This fictional form is for import and report stress testing only.")
    page.save()
    return path


def main():
    DESTINATION.mkdir(parents=True, exist_ok=True)
    data = list(rows())
    csv_path, pdf_path = write_csv(data), write_form(data)
    print(f"Created {len(data)} same-schema rows: {csv_path.name}; {pdf_path.name}")


if __name__ == "__main__":
    main()
