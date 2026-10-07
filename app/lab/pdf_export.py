"""Certificate-quality PDF from a frozen report snapshot; no invented readings."""

import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    KeepTogether,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)

from .report_analysis import analysis

NAVY = colors.HexColor("#171717")
TEAL = colors.HexColor("#333333")
LIGHT = colors.HexColor("#eeeeee")
RULE = colors.HexColor("#bbbbbb")
INK = colors.HexColor("#171717")
FONT = "VectorDejaVu"
pdfmetrics.registerFont(TTFont(FONT, str(Path(__file__).with_name("fonts") / "DejaVuSans.ttf")))
pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT, italic=FONT, boldItalic=FONT)


class NumberedCanvas(canvas.Canvas):
    def __init__(
        self,
        *args,
        certificate="",
        sample="",
        revision=1,
        provisional=False,
        mapping_version=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        (
            self.certificate,
            self.sample,
            self.revision,
            self.provisional,
            self.mapping_version,
            self.saved,
        ) = (certificate, sample, revision, provisional, mapping_version, [])

    def showPage(self):
        self.saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        count = len(self.saved)
        for state in self.saved:
            self.__dict__.update(state)
            if self.provisional:
                self.saveState()
                self.translate(295, 420)
                self.rotate(38)
                self.setFillColor(colors.HexColor("#dedede"))
                self.setFont(FONT, 27)
                self.drawCentredString(0, 0, "AUTOMATED DRAFT - NOT ISSUED")
                self.restoreState()
            self.setStrokeColor(RULE)
            self.line(45, 808, 550, 808)
            self.line(45, 47, 550, 47)
            self.setFillColor(NAVY)
            self.setFont(FONT, 7)
            self.drawString(45, 825, "CENTRAL POWER RESEARCH INSTITUTE")
            self.drawString(45, 815, "Short Circuit Laboratory, Bengaluru")
            self.setFont(FONT, 7)
            self.drawRightString(550, 825, f"Test Report No. {self.certificate}")
            self.drawRightString(550, 815, f"Rev {self.revision}")
            template_label = "CPRI-SCL-TR-v1" + (
                f" · mapping v{self.mapping_version}" if self.mapping_version else ""
            )
            self.drawString(45, 35, f"Sample {self.sample}  |  Template {template_label}")
            self.drawRightString(550, 35, f"Page {self._pageNumber} of {count}")
            self.setFont(FONT, 6)
            self.drawString(
                45,
                24,
                "This report shall not be reproduced except in full without written approval of CPRI.",
            )
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)


def shown(value):
    try:
        value = Decimal(str(value))
        if value.is_finite():
            return format(value.quantize(Decimal("0.001")), "f").rstrip("0").rstrip(".") or "0"
    except (InvalidOperation, TypeError, ValueError):
        pass
    return str(value if value is not None else "")


def measured_text(value, unit=""):
    if value is None:
        return "Pending review"
    if str(unit).strip().casefold() in ("w", "watt", "watts"):
        try:
            return f"{Decimal(str(value)):.2f}"
        except (InvalidOperation, TypeError, ValueError):
            pass
    return shown(value)


def render_pdf(report):
    if ((report.snapshot.get("template_mapping") or {}).get("definition") or {}).get(
        "format"
    ) == "CPRI-SCL-TR-v1":
        from .fixed_report import render_fixed_pdf

        return render_fixed_pdf(report)
    data = dict(report.snapshot)
    if data.get("source_checked_reactance_limit") is not None:
        worksheet_rows = {
            row.get("stage"): row
            for calculation in data.get("transformer_calculations", [])
            for row in calculation.get("rows", [])
        }
        revised = []
        for original in data.get("calculations", []):
            item = dict(original)
            match = re.fullmatch(r"REACTANCE_(HT|NT|LT)BT_\1AT", item.get("code", ""))
            if match:
                tap = match.group(1)
                before = (worksheet_rows.get(tap + "BT") or {}).get("X50")
                after = (worksheet_rows.get(tap + "AT") or {}).get("X50")
                if before is not None and after is not None and Decimal(str(before)) != 0:
                    change = (
                        (Decimal(str(after)) - Decimal(str(before))) / Decimal(str(before)) * 100
                    )
                    limit = Decimal(str(data["source_checked_reactance_limit"]))
                    item.update(
                        value=float(abs(change)),
                        limit=float(limit),
                        margin=float(limit - abs(change)),
                        unit="%",
                        margin_unit="%",
                        verdict="pass" if abs(change) <= limit else "fail",
                    )
            revised.append(item)
        data["calculations"] = revised
    provisional = bool(data.get("provisional_preview"))
    if provisional and report.approved_at:
        raise ValueError("A provisional preview cannot be issued or signed.")
    if provisional and data.get("provisional_analysis_fields") is not None:
        detail = analysis(dict(data, fields=data["provisional_analysis_fields"]))
    elif data.get("source_checked_reactance_limit") is not None:
        detail = analysis(data)
    else:
        detail = data.get("report_analysis") or analysis(data)
    issued = bool(report.approved_at)
    certificate = data.get("file_number") or "Pending allocation"
    sample = data.get("sample_code") or "Not recorded"
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            "CertTitle",
            parent=styles["Title"],
            fontName=FONT,
            fontSize=18,
            leading=22,
            textColor=NAVY,
            spaceAfter=12,
        )
    )
    styles.add(
        ParagraphStyle(
            "CertH1",
            parent=styles["Heading1"],
            fontName=FONT,
            fontSize=12.5,
            leading=16,
            textColor=NAVY,
            spaceBefore=4,
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            "CertH2",
            parent=styles["Heading2"],
            fontName=FONT,
            fontSize=9.5,
            leading=12,
            textColor=NAVY,
            spaceBefore=8,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            "CertBody",
            parent=styles["BodyText"],
            fontName=FONT,
            fontSize=8.1,
            leading=10.6,
            textColor=INK,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            "CertSmall", parent=styles["CertBody"], fontSize=7.1, leading=9.2, spaceAfter=3
        )
    )
    styles.add(
        ParagraphStyle("CertCell", parent=styles["CertBody"], fontSize=7, leading=8.8, spaceAfter=0)
    )
    styles.add(ParagraphStyle("CertNum", parent=styles["CertCell"], alignment=TA_RIGHT))
    styles.add(ParagraphStyle("CertCenter", parent=styles["CertCell"], alignment=TA_CENTER))

    def p(value, style="CertBody"):
        value = re.sub(
            r"\bwithheld\b", "pending review", str(value if value is not None else ""), flags=re.I
        )
        if provisional:
            value = re.sub(r"\breviewed\b", "extracted", value, flags=re.I)
            value = re.sub(r"\bunreviewed\b", "unverified", value, flags=re.I)
        return Paragraph(escape(value).replace("\n", "<br/>"), styles[style])

    def grid(rows, widths, numeric=(), header=True):
        cells = [
            [
                p(
                    value,
                    (
                        "CertCenter"
                        if index == 0 and header
                        else "CertNum" if column in numeric else "CertCell"
                    ),
                )
                for column, value in enumerate(row)
            ]
            for index, row in enumerate(rows)
        ]
        table = LongTable(cells, colWidths=widths, repeatRows=1 if header else 0, splitByRow=1)
        style = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
        ]
        if header:
            style += [
                ("BACKGROUND", (0, 0), (-1, 0), LIGHT),
                ("LINEBELOW", (0, 0), (-1, 0), 0.7, TEAL),
            ]
        table.setStyle(TableStyle(style))
        return table

    documents = {doc["id"]: doc["name"] for doc in data.get("documents", [])}

    def source(field):
        name = documents.get(field.get("document_id"))
        if name:
            return name + (f", p. {field['page']}" if field.get("page") else "")
        return field.get("source") or "Digital station entry"

    def reading(kind, key):
        matches = [
            field
            for field in data.get("fields", [])
            if field.get("form_type") == kind
            and field.get("schema_key") == key
            and field.get("status") != "not_applicable"
            and field.get("extraction_status") != "struck_out"
            and not (provisional and field.get("preview_excluded"))
            and str(field.get("value", "")).strip()
        ]
        return matches[0] if len(matches) == 1 else None

    def reviewed(kind, key):
        field = reading(kind, key)
        return field if field and (field.get("status") == "verified" or provisional) else None

    def cover_value(options, fallback=None, unit=None):
        for kind, key in options:
            field = reading(kind, key)
            if field:
                chosen_unit = field.get("unit") or unit
                value = field["value"] + (f" {chosen_unit}" if chosen_unit else "")
                return value + (
                    " [extracted, unverified]" if field.get("status") != "verified" else ""
                )
        return fallback or "Not recorded"

    cover_facts = data.get("source_checked_cover") or {}

    def cover_fact(name, options=(), fallback=None, unit=None):
        return (
            str(cover_facts[name])
            if cover_facts.get(name)
            else cover_value(options, fallback, unit)
        )

    def reading_tables(section):
        scalar, grouped = [], defaultdict(lambda: defaultdict(dict))
        for field in section.get("fields", []):
            match = re.fullmatch(r"(.+)\.(\d+)\.([^.]+)", field.get("schema_key", ""))
            if match:
                prefix, index, column = match.groups()
                grouped[(field.get("document_id") or field.get("source"), prefix)][int(index)][
                    column
                ] = field
            else:
                scalar.append(field)
        if scalar:
            rows = [["Reading", "Result", "Unit", "Review", "Source"]]
            for field in scalar:
                state = field.get("status", "")
                value = "N/A" if state == "not_applicable" else field.get("value") or "Not recorded"
                review = (
                    "Struck out"
                    if field.get("extraction_status") == "struck_out"
                    else {
                        "verified": "Checked",
                        "not_applicable": "N/A",
                        "unreviewed": "Unverified",
                        "ambiguous": "Unclear",
                    }.get(state, state)
                )
                rows.append(
                    [
                        field.get("label") or field.get("schema_key"),
                        value,
                        field.get("unit") or "",
                        review,
                        source(field),
                    ]
                )
            yield grid(rows, [132, 118, 35, 48, 172], numeric=(1,))
        for (_, prefix), records in grouped.items():
            yield p(prefix.replace("_", " ").title(), "CertH2")
            columns = list(dict.fromkeys(col for record in records.values() for col in record))
            for start in range(0, len(columns), 5):
                subset = columns[start : start + 5]
                rows = [["Row"] + [col.replace("_", " ") for col in subset]]
                for index, record in sorted(records.items()):
                    cells = [str(index + 1)]
                    for col in subset:
                        field = record.get(col)
                        if field is None:
                            cells.append("Not recorded")
                        elif field.get("status") == "not_applicable":
                            cells.append("N/A")
                        elif field.get("extraction_status") == "struck_out":
                            cells.append((field.get("value") or "Not recorded") + " [struck out]")
                        else:
                            cells.append(
                                (field.get("value") or "Not recorded")
                                + (" [review]" if field.get("status") != "verified" else "")
                            )
                    rows.append(cells)
                yield grid(
                    rows,
                    [40] + [465 / len(subset)] * len(subset),
                    numeric=tuple(range(1, len(subset) + 1)),
                )
            first = next((field for record in records.values() for field in record.values()), None)
            if first:
                yield p("Source: " + source(first), "CertSmall")

    def rise_chart():
        curve = []
        for index in range(30):
            fields = [
                (reading if provisional or data.get("chart_allow_unreviewed") else reviewed)(
                    "temperature_rise", f"time_series.{index}.{key}"
                )
                for key in ("top_oil", "bottom_oil", "ambient_1", "ambient_2", "ambient_3")
            ]
            try:
                if all(fields):
                    values = [float(field["value"]) for field in fields]
                    curve.append((values[0], values[1], sum(values[2:]) / 3))
            except (TypeError, ValueError):
                pass
        if len(curve) >= 2:
            chart = HorizontalLineChart()
            chart.x = 55
            chart.y = 40
            chart.width = 385
            chart.height = 140
            chart.data = [[row[column] for row in curve] for column in range(3)]
            chart.categoryAxis.categoryNames = [str(index + 1) for index in range(len(curve))]
            chart.valueAxis.valueMin = 0
            chart.lines[0].strokeColor = TEAL
            chart.lines[1].strokeColor = NAVY
            chart.lines[2].strokeColor = colors.HexColor("#999999")
            drawing = Drawing(500, 195)
            drawing.add(chart)
            caption = (
                "Heating curve: top oil (black), bottom oil (dark grey), mean ambient (light grey)."
            )
            if data.get("chart_allow_unreviewed"):
                caption += " Source time series is unreviewed; this trend is provisional."
            return [p(caption, "CertSmall"), drawing]
        pairs = []
        for label, key, guarantee in (
            ("Top oil", "top_oil_rise", "guaranteed_temp_rise_1"),
            ("HV winding", "hv_winding_rise", "guaranteed_temp_rise_2"),
            ("LV winding", "lv_winding_rise", "guaranteed_temp_rise_2"),
        ):
            selected = reading if provisional else reviewed
            measured, limit = selected("temperature_rise", key), selected(
                "transformer_proforma", guarantee
            )
            try:
                if measured and limit:
                    pairs.append((label, float(measured["value"]), float(limit["value"])))
            except (TypeError, ValueError):
                pass
        if not pairs:
            return [
                p(
                    "Rise comparison chart withheld: reviewed rise and guarantee pairs are incomplete.",
                    "CertSmall",
                )
            ]
        chart = VerticalBarChart()
        chart.x = 52
        chart.y = 37
        chart.width = 390
        chart.height = 140
        chart.data = [[pair[1] for pair in pairs], [pair[2] for pair in pairs]]
        chart.categoryAxis.categoryNames = [pair[0] for pair in pairs]
        chart.valueAxis.valueMin = 0
        chart.valueAxis.valueMax = max(max(pair[1], pair[2]) for pair in pairs) * 1.2
        chart.bars[0].fillColor = TEAL
        chart.bars[1].fillColor = colors.HexColor("#999999")
        drawing = Drawing(500, 195)
        drawing.add(chart)
        return [p("Measured rise (dark) and recorded guarantee (light), K.", "CertSmall"), drawing]

    def engineering_rows(kind):
        members = [item for item in data.get("calculations", []) if item.get("test_type") == kind]
        if not members:
            return [p("Acceptance rule not configured for this test.", "CertSmall")]
        rows = [["Engineering check", "Measured", "Limit", "Margin", "Result"]]
        for item in members:
            unit = item.get("unit") or item.get("margin_unit") or ""
            measured = (
                measured_text(item["value"], unit) + (" " + unit if unit else "")
                if item.get("value") is not None
                else "Pending review"
            )
            limit = (
                measured_text(item["limit"], unit) + (" " + unit if unit else "")
                if item.get("limit") is not None
                else "Not recorded"
            )
            margin = (
                shown(item["margin"]) + (" " + (item.get("margin_unit") or unit))
                if item.get("margin") is not None
                else "Withheld"
            )
            state = (item.get("verdict") or "blocked").upper()
            if item.get("code") in data.get("descriptive_rule_codes", []):
                state = "DESCRIPTIVE"
                limit = "Not assigned"
                margin = "—"
            elif item.get("rule_status") != "confirmed" and state in ("PASS", "MARGINAL", "FAIL"):
                state = "PROVISIONAL " + state
            rows.append([item.get("title") or "Engineering check", measured, limit, margin, state])
        return [grid(rows, [178, 85, 78, 75, 89], numeric=(1, 2, 3))]

    def ratio_table():
        rows = [["Tap", "Before mean", "After mean", "Change (%)", "Evidence"]]
        for index in range(7):
            tap = reading("routine_test", f"ratio.{index}.tap")
            if not tap:
                continue
            readings = [
                [reading("routine_test", f"ratio.{index}.{stage}_{phase}") for phase in "ABC"]
                for stage in ("BT", "AT")
            ]
            allowed = lambda field: field and (provisional or field.get("status") == "verified")
            means = []
            suspect = False
            for group in readings:
                try:
                    values = (
                        [Decimal(str(field["value"])) for field in group]
                        if all(allowed(field) for field in group)
                        else []
                    )
                    if values and min(values) > 0 and max(values) / min(values) > Decimal("1.05"):
                        suspect = True
                        means.append(None)
                    else:
                        means.append(sum(values) / Decimal(3) if values else None)
                except (InvalidOperation, ValueError, TypeError):
                    means.append(None)
            deviation = (
                abs(means[1] - means[0]) / abs(means[0]) * 100
                if all(value is not None for value in means) and means[0]
                else None
            )
            rows.append(
                [
                    tap["value"],
                    shown(means[0]) if means[0] is not None else "Pending review",
                    shown(means[1]) if means[1] is not None else "Pending review",
                    shown(deviation) if deviation is not None else "Pending review",
                    source(tap) + ("; phase outlier: check source" if suspect else ""),
                ]
            )
        if len(rows) == 1:
            return []
        return [
            p(
                "Voltage ratio by tap: before/after phase mean and change. Nominal-ratio deviation is withheld until the declared tap ratios are confirmed.",
                "CertSmall",
            ),
            grid(rows, [42, 95, 95, 83, 190], numeric=(1, 2, 3)),
        ]

    def heating_table():
        rows = [["Hour / time", "Top oil (°C)", "Bottom oil (°C)", "Ambient (°C)", "Source"]]
        for index in range(30):
            if (
                provisional
                and index in (13, 14)
                and any(
                    field.get("form_type") == "temperature_rise"
                    and field.get("schema_key", "").startswith(f"time_series.{index}.")
                    for field in data.get("fields", [])
                )
            ):
                rows.append(
                    [
                        f"Row {index+1}",
                        "Withheld",
                        "Withheld",
                        "Withheld",
                        "Column alignment needs review",
                    ]
                )
                continue
            entries = [
                reading("temperature_rise", f"time_series.{index}.{key}")
                for key in ("time", "top_oil", "bottom_oil", "ambient_1")
            ]
            if not any(entries):
                continue

            def cell(field):
                if not field:
                    return "Not recorded"
                return (
                    field["value"] if provisional or field["status"] == "verified" else "Withheld"
                )

            rows.append(
                [
                    cell(entries[0]),
                    cell(entries[1]),
                    cell(entries[2]),
                    cell(entries[3]),
                    source(next(field for field in entries if field)),
                ]
            )
        return [grid(rows, [70, 85, 95, 90, 165], numeric=(1, 2, 3))] if len(rows) > 1 else []

    def worksheet_table():
        rows = [
            ["Tap", "Load loss (W)", "Z75 (%)", "Stray (W)", "50 % total (W)", "100 % total (W)"]
        ]
        for calculation in data.get("transformer_calculations", []):
            for row in calculation.get("rows", []):
                rows.append(
                    [
                        row.get("stage", "Not recorded"),
                        measured_text(row.get("load_loss_100"), "W"),
                        shown(row.get("Z75")),
                        measured_text(row.get("stray_loss"), "W"),
                        measured_text(row.get("total_loss_50"), "W"),
                        measured_text(row.get("total_loss_100"), "W"),
                    ]
                )
        return (
            [grid(rows, [64, 90, 72, 75, 102, 102], numeric=(1, 2, 3, 4, 5))]
            if len(rows) > 1
            else []
        )

    story = [
        Spacer(1, 14),
        p("TEST REPORT", "CertTitle"),
        p("ISSUED CERTIFICATE" if issued else "DRAFT - REVIEW REQUIRED", "CertH1"),
        p(data.get("report_title") or "Laboratory test report", "CertSmall"),
        p(
            "Report No. "
            + certificate
            + "    |    File No. "
            + certificate
            + f"    |    Revision {report.revision}",
            "CertSmall",
        ),
        p(
            "Date of issue: "
            + (report.approved_at.strftime("%d %B %Y") if issued else "Not issued"),
            "CertSmall",
        ),
        Spacer(1, 8),
    ]
    request = data.get("request_snapshot") or {}

    def submitted(name, options):
        if cover_facts.get(name):
            return str(cover_facts[name])
        if request.get(name):
            return str(request[name])
        return cover_value(options, data.get(name))

    rating = cover_facts.get("rating") or " / ".join(
        cover_value(
            [("customer_request", key), ("transformer_proforma", key), ("temperature_rise", key)],
            unit=unit,
        )
        for key, unit in (("rated_power", "kVA"), ("rated_hv", "V"), ("rated_lv", "V"))
    )
    standard = cover_fact(
        "standard",
        [
            ("transformer_proforma", "standard"),
            ("temperature_rise", "standard"),
            ("routine_test", "standard"),
        ],
    )
    requested_tests = (
        cover_facts.get("tests_requested")
        or request.get("requested_tests")
        or cover_value(
            [("customer_request", "requested_test")],
            ", ".join(str(value).replace("_", " ").title() for value in data.get("scope", [])),
        )
    )
    cover = [
        [
            "Customer:",
            submitted(
                "customer",
                [("customer_request", "customer_name"), ("temperature_rise", "customer")],
            ),
        ],
        [
            "Customer address:",
            submitted("customer_address", [("customer_request", "customer_address")]),
        ],
        ["Manufacturer:", submitted("manufacturer", [("customer_request", "manufacturer")])],
        ["Manufacturer address:", "Not recorded"],
        [
            "Sample particulars:",
            submitted(
                "sample_particulars",
                [
                    ("customer_request", "sample_description"),
                    ("transformer_proforma", "description"),
                ],
            ),
        ],
        [
            "Sample code:",
            cover_fact(
                "sample_code",
                [
                    ("customer_request", "sample_code"),
                    ("transformer_proforma", "sample_code"),
                    ("routine_test", "sample_code"),
                    ("temperature_rise", "sample_code"),
                ],
                sample if issued else None,
            ),
        ],
        [
            "Test series no.:",
            cover_fact(
                "test_series",
                [
                    ("transformer_proforma", "test_series"),
                    ("routine_test", "series"),
                    ("temperature_rise", "series"),
                ],
                data.get("test_series") if issued else None,
            ),
        ],
        ["Rating:", rating],
        [
            "Vector group:",
            cover_value(
                [("transformer_proforma", "vector_group"), ("routine_test", "vector_group_BT")]
            ),
        ],
        ["Cooling:", cover_value([("transformer_proforma", "cooling")])],
        [
            "Serial no.:",
            cover_fact(
                "serial_number",
                [
                    ("customer_request", "serial_number"),
                    ("transformer_proforma", "serial_number"),
                    ("routine_test", "serial"),
                ],
            ),
        ],
        ["Receipt / condition:", "Not recorded / Not recorded"],
        ["Test period:", "See dated station records; consolidated dates not recorded"],
        ["Witnessed by:", "Not recorded"],
        ["Standard(s):", standard],
        ["Tests requested:", requested_tests or "Not recorded"],
    ]
    overall_statement = data.get("draft_overall_statement") if not issued else None
    story += [
        grid(cover, [140, 365], header=False),
        Spacer(1, 9),
        p("OVERALL RESULT", "CertH2"),
        p(overall_statement or detail["overall"]["text"]),
        p("Basis: Summary of Results and linked test sections.", "CertSmall"),
    ]
    if not issued:
        story.append(
            p(
                "Not issued: remaining source review, rule approval and authorisation are pending.",
                "CertSmall",
            )
        )
    signatures = [
        ["Test Engineer", "Quality", "HoD digital signature"],
        [
            str(report.engineer_locked_by) if issued else "Pending",
            str(report.quality_verified_by) if issued else "Pending",
            str(report.approved_by) if issued else "Pending",
        ],
    ]
    story += [Spacer(1, 7), p("Authorisation", "CertH2"), grid(signatures, [168, 168, 169])]
    if issued:
        from .report_workflow import issue_code

        code = issue_code(report)
        url = (
            f"{settings.PUBLIC_VERIFY_BASE_URL}/verify/{report.pk}/{code}/"
            if settings.PUBLIC_VERIFY_BASE_URL
            else f"OV-VERIFY:{report.pk}:{code}"
        )
        widget = QrCodeWidget(url)
        x0, y0, x1, y1 = widget.getBounds()
        drawing = Drawing(64, 64, transform=[64 / (x1 - x0), 0, 0, 64 / (y1 - y0), -x0, -y0])
        drawing.add(widget)
        story += [Spacer(1, 6), drawing, p("Scan to verify this issued certificate.", "CertSmall")]
    story.append(PageBreak())

    story += [
        p("SUMMARY OF RESULTS", "CertH1"),
        p(
            "Each row summarises one test; the smallest signed margin identifies the worst tap.",
            "CertSmall",
        ),
    ]
    verdicts = data.get("calculations", [])

    def matching_rules(kind, words):
        return [
            item
            for item in verdicts
            if item.get("test_type") == kind
            and any(word in (item.get("title") or "").casefold() for word in words)
        ]

    categories = [
        ("Winding resistance", "routine_test", ("winding resistance",)),
        ("Voltage ratio / vector group", "routine_test", ("ratio", "vector group")),
        ("Insulation resistance", "routine_test", ("insulation resistance",)),
        ("Dielectric withstand", "routine_test", ("dielectric", "separate-source")),
        ("No-load current", "loss_measurement", ("no-load",)),
        ("Load loss at 75 °C", "loss_calculation", ("load loss",)),
        ("Impedance at 75 °C", "loss_calculation", ("impedance",)),
        ("Total loss at 50 % load", "loss_calculation", ("total loss at 50",)),
        ("Total loss at 100 % load", "loss_calculation", ("total loss at 100",)),
        ("Temperature rise", "temperature_rise", ("rise",)),
        ("Short-circuit withstand", "short_circuit", ("reactance", "short-circuit")),
        ("Pressure / vacuum", "pressure_oil_leakage", ("pressure", "deflection")),
        ("Oil leakage", "pressure_oil_leakage", ("leakage",)),
    ]
    rows = [["Sl", "Test", "Requirement", "Measured range", "Worst tap", "Margin", "Result"]]
    for index, (label, kind, words) in enumerate(categories, 1):
        members = matching_rules(kind, words)
        active = [
            item
            for item in members
            if item.get("code") not in data.get("descriptive_rule_codes", [])
        ]
        evaluated = [
            item
            for item in active
            if item.get("value") is not None
            and item.get("verdict") not in ("blocked", "not_applicable")
        ]
        ranked = sorted(
            evaluated,
            key=lambda item: (
                Decimal(str(item["margin"]))
                if item.get("margin") is not None
                else Decimal("Infinity")
            ),
        )
        worst = ranked[0] if ranked else None
        nums = []
        for item in evaluated:
            try:
                nums.append(Decimal(str(item["value"])))
            except (InvalidOperation, ValueError, TypeError):
                pass
        unit = (worst or (active[0] if active else {})).get("unit") or ""
        span = (
            (
                measured_text(min(nums), unit)
                if min(nums) == max(nums)
                else f"{measured_text(min(nums),unit)} to {measured_text(max(nums),unit)}"
            )
            if nums
            else "Pending review"
        )
        if nums and unit:
            span += " " + unit
        tap = re.search(r"\b(?:HT|NT|LT)(?:BT|AT)\b", (worst or {}).get("title", ""), re.I)
        if not members:
            state = (
                "NOT REQUESTED"
                if data.get("scope") and kind not in data["scope"]
                else "DESCRIPTIVE"
            )
        elif not active:
            state = "DESCRIPTIVE"
        elif any(item.get("verdict") == "fail" for item in active):
            state = "FAIL"
        elif any(item.get("verdict") == "blocked" for item in active):
            state = "PENDING REVIEW"
        elif any(item.get("verdict") == "marginal" for item in active):
            state = "MARGINAL"
        elif all(item.get("verdict") == "not_applicable" for item in active):
            state = "NOT REQUESTED"
        else:
            state = "PASS"
        if state in ("PASS", "MARGINAL", "FAIL"):
            state = {"PASS": "PASS ✓", "MARGINAL": "PASS (MARGINAL) ⚠", "FAIL": "FAIL ✗"}[state]
            if any(item.get("rule_status") != "confirmed" for item in active):
                state = "PROVISIONAL " + state
        rows.append(
            [
                index,
                label,
                (
                    measured_text(worst["limit"], unit) + (" " + unit if unit else "")
                    if worst and worst.get("limit") is not None
                    else "Not assigned"
                ),
                span if members else "See test section",
                tap.group(0) if tap else "-",
                (
                    measured_text(worst["margin"], worst.get("margin_unit") or unit)
                    + (" " + (worst.get("margin_unit") or unit) if worst else "")
                    if worst and worst.get("margin") is not None
                    else "—"
                ),
                state,
            ]
        )
    story.append(grid(rows, [22, 118, 72, 83, 52, 68, 90], numeric=(0, 2, 3, 5)))
    story.append(
        p(
            "HT/LT loss and impedance readings are descriptive; acceptance at non-principal taps requires a declared limit. All displayed verdicts remain provisional until rule approval.",
            "CertSmall",
        )
    )
    if not verdicts:
        story.append(p("No engineering checks assigned; conformity withheld.", "CertSmall"))
    story.append(PageBreak())

    configured = data.get("report_sections") or []
    sections = data.get("sections") or configured
    by_type = {section.get("key"): dict(section) for section in sections}
    for mapped_section in configured:
        kind = mapped_section.get("key")
        if kind not in by_type:
            by_type[kind] = dict(mapped_section)
            continue
        replacements = {field.get("id"): field for field in mapped_section.get("fields", [])}
        by_type[kind]["title"] = mapped_section.get("title") or by_type[kind].get("title")
        by_type[kind]["fields"] = [
            replacements.get(field.get("id"), field) for field in by_type[kind].get("fields", [])
        ]
    requested = set(data.get("scope") or [])
    order = [
        ("transformer_proforma", "SAMPLE DESCRIPTION"),
        ("routine_test", "ROUTINE TESTS"),
        ("loss_measurement", "NO-LOAD LOSS AND CURRENT"),
        ("loss_calculation", "LOAD LOSS, IMPEDANCE AND TOTAL LOSS"),
        ("temperature_rise", "TEMPERATURE RISE TEST"),
        ("short_circuit", "SHORT-CIRCUIT WITHSTAND TEST"),
        ("pressure_oil_leakage", "PRESSURE AND OIL LEAKAGE TESTS"),
    ]
    for index, (kind, heading) in enumerate(order):
        section = by_type.get(kind, {"key": kind, "fields": [], "missing": []})
        if index:
            story.append(Spacer(1, 16))
        kind = section.get("key", "")
        story.append(p(heading, "CertH1"))
        if section.get("title") and section["title"] != heading:
            story.append(p(section["title"], "CertSmall"))
        if requested and kind not in requested and not (kind == "transformer_proforma"):
            story.append(p("Not requested."))
            continue
        for label, key in (
            ("Condition", "condition"),
            ("Method", "method"),
            ("Standard", "standard"),
            ("Test date", "date"),
        ):
            field = reviewed(kind, key)
            if field and label == "Test date":
                raw = str(field["value"]).strip()
                try:
                    datetime.strptime(raw.replace("/", "-").replace(".", "-"), "%d-%m-%Y")
                except ValueError:
                    story.append(
                        p(f"Test date: source check required ({source(field)}).", "CertSmall")
                    )
                    continue
            if field:
                story.append(
                    p(
                        f"{label}: {field['value']} {field.get('unit','')}. Source: {source(field)}",
                        "CertSmall",
                    )
                )
        story.append(p("Engineering results", "CertH2"))
        if kind == "transformer_proforma":
            rows = [["Nameplate item", "Recorded value", "Source"]]
            for label, key, unit in (
                ("Rated power", "rated_power", "kVA"),
                ("HV voltage", "rated_hv", "V"),
                ("LV voltage", "rated_lv", "V"),
                ("Frequency", "frequency", "Hz"),
                ("Vector group", "vector_group", ""),
                ("Cooling", "cooling", ""),
                ("Serial number", "serial_number", ""),
                ("Guaranteed impedance", "impedance_at_75", "%"),
                ("50 % loss guarantee", "guaranteed_total_loss_50", "W"),
                ("100 % loss guarantee", "guaranteed_total_loss_100", "W"),
            ):
                field = reading(kind, key)
                value = (
                    measured_text(field["value"], unit)
                    if field and unit == "W"
                    else field["value"] if field else ""
                )
                rows.append(
                    [
                        label,
                        (
                            (
                                value
                                + (" " + unit if unit else "")
                                + (" [unreviewed]" if field["status"] != "verified" else "")
                            )
                            if field
                            else "Not recorded"
                        ),
                        source(field) if field else "Not recorded",
                    ]
                )
            story.append(grid(rows, [155, 130, 220], numeric=(1,)))
        else:
            story.extend(engineering_rows(kind))
        if kind == "loss_calculation":
            story.append(
                p(
                    "Copper resistance correction: R75 = Rt x (235 + 75)/(235 + t). "
                    "Load-loss correction: P75 = Pcu,t x f + (Ptest - Pcu,t)/f; f = 310/(235 + t). "
                    "Calculation profile: supplied v2.17 worksheet.",
                    "CertSmall",
                )
            )
            story.extend(worksheet_table())
        if kind == "temperature_rise":
            story.append(
                p(
                    "Resistance-method winding rise: θ = (R2/R1)(235 + θ1) - 235 - θa + correction. "
                    "A numerical reconstruction is withheld unless the required reviewed inputs and correction method are configured. "
                    "Method reference: IS 2026-2, applicability to be confirmed.",
                    "CertSmall",
                )
            )
            story.extend(heating_table())
        if kind == "routine_test":
            story.extend(ratio_table())
        entries = [
            entry
            for entry in detail["sections"].get(kind, [])
            if not any(
                entry["text"].startswith((item.get("title") or "") + ":")
                for item in verdicts
                if item.get("test_type") == kind
            )
        ]
        if entries:
            for entry in entries:
                story += [
                    p(entry["text"]),
                    p("Evidence: " + "; ".join(entry["references"]), "CertSmall"),
                ]
        elif kind != "transformer_proforma":
            story.append(p("No additional reviewed test narrative is available.", "CertSmall"))
        if kind == "temperature_rise":
            story.append(
                KeepTogether([p("Heating-curve and rise comparison", "CertH2"), *rise_chart()])
            )
        if section.get("missing"):
            story.append(
                p(f"{len(section['missing'])} configured readings are not recorded.", "CertSmall")
            )

    story += [PageBreak(), p("CROSS-TEST CONSISTENCY", "CertH1")]
    selected = reading if provisional else reviewed
    comparisons = []

    def add_comparison(label, before, after, unit):
        try:
            baseline = Decimal(str(before)) if before is not None else None
            final = Decimal(str(after)) if after is not None else None
            change = abs(final - baseline) / abs(baseline) * 100 if baseline else None
        except (InvalidOperation, ValueError, TypeError):
            change = None
        comparisons.append(
            [
                label,
                shown(before) + " " + unit if before is not None else "Withheld",
                shown(after) + " " + unit if after is not None else "Withheld",
                shown(change) + " %" if change is not None else "Withheld",
            ]
        )

    for label, prefix, unit in (
        ("HV-earth IR", "ir_hv_earth", "GΩ"),
        ("LV-earth IR", "ir_lv_earth", "GΩ"),
        ("HV-LV IR", "ir_hv_lv", "GΩ"),
    ):
        before, after = (selected("routine_test", prefix + suffix) for suffix in ("_BT", "_AT"))
        add_comparison(
            label, before["value"] if before else None, after["value"] if after else None, unit
        )
    for label, prefix, unit in (
        ("HV resistance, normal tap", "hv_resistance.1", "Ω"),
        ("LV resistance", "lv_resistance.0", "mΩ"),
    ):
        values = []
        for stage in ("BT", "AT"):
            group = [
                selected("loss_measurement", f"{prefix}.{stage}_{phase}") for phase in (1, 2, 3)
            ]
            try:
                values.append(
                    sum(Decimal(field["value"]) for field in group) / Decimal(3)
                    if all(group)
                    else None
                )
            except (InvalidOperation, ValueError, TypeError):
                values.append(None)
        add_comparison(label, *values, unit)
    taps = {
        row.get("stage"): row
        for calculation in data.get("transformer_calculations", [])
        for row in calculation.get("rows", [])
    }
    for metric, label, unit in (
        ("Z75", "Impedance, normal tap", "%"),
        ("X50", "Reactance, normal tap", "%"),
    ):
        add_comparison(
            label, (taps.get("NTBT") or {}).get(metric), (taps.get("NTAT") or {}).get(metric), unit
        )
    story.append(
        grid(
            [["Parameter", "Before SC", "After SC", "Change"]] + comparisons,
            [200, 100, 100, 105],
            numeric=(1, 2, 3),
        )
    )
    reactance_limit = data.get("source_checked_reactance_limit")
    if reactance_limit is not None:
        rows = [["Tap", "Before X (%)", "After X (%)", "Change (%)", "Result"]]
        for tap in ("HT", "NT", "LT"):
            before = (taps.get(tap + "BT") or {}).get("X50")
            after = (taps.get(tap + "AT") or {}).get("X50")
            if before is None or after is None:
                continue
            change = (Decimal(str(after)) - Decimal(str(before))) / Decimal(str(before)) * 100
            state = (
                "PROVISIONAL PASS ✓"
                if abs(change) <= Decimal(str(reactance_limit))
                else "PROVISIONAL FAIL ✗"
            )
            rows.append([tap, shown(before), shown(after), f"{change:+.3f}", state])
        if len(rows) > 1:
            story += [
                p("Reactance after short circuit", "CertH2"),
                p(
                    f"Candidate acceptance: absolute change ≤ {reactance_limit}%; source and rule approval pending.",
                    "CertSmall",
                ),
                grid(rows, [48, 98, 98, 96, 165], numeric=(1, 2, 3)),
            ]
    story.append(
        p(
            "A before/after change is descriptive; conformity is determined only by the separately configured acceptance rule.",
            "CertSmall",
        )
    )
    story += [p("OBSERVATIONS AND REMARKS", "CertH1")]
    observations = [item for item in verdicts if item.get("verdict") in ("marginal", "fail")]
    if observations:
        seen = set()
        for item in observations:
            title = item.get("title") or "Engineering check"
            fingerprint = (
                title,
                item.get("verdict"),
                str(item.get("value")),
                str(item.get("margin")),
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            status = item["verdict"].upper()
            if item.get("rule_status") != "confirmed":
                status = "PROVISIONAL " + status
            remark = (
                f"{status}: measured {shown(item.get('value'))} "
                f"{item.get('unit') or ''}; limit {shown(item.get('limit'))} "
                f"{item.get('unit') or ''}; signed margin {shown(item.get('margin'))} "
                f"{item.get('margin_unit') or item.get('unit') or ''}."
            )
            story.append(p(f"{title}: {remark}"))
    elif any(item.get("verdict") == "blocked" for item in verdicts):
        story.append(
            p(
                "Rule evaluation remains withheld because required evidence or configuration is incomplete."
            )
        )
    else:
        story.append(p("No marginal or failed rule results recorded."))

    story += [PageBreak(), p("ANNEXURES", "CertH1"), p("A. Source records", "CertH2")]
    docs = data.get("documents", [])
    if docs:
        story.append(
            grid(
                [["Sl", "Source document", "State"]]
                + [
                    [i, doc["name"], doc.get("status") or "Recorded"]
                    for i, doc in enumerate(docs, 1)
                ],
                [30, 390, 85],
                numeric=(0,),
            )
        )
    else:
        story.append(p("No source documents attached."))
    story.append(p("B. Instruments", "CertH2"))
    instruments = [
        f
        for f in data.get("fields", [])
        if f.get("schema_key") == "instrument_serials"
        and f.get("status") == "verified"
        and str(f.get("value", "")).strip()
    ]
    if instruments:
        for field in instruments:
            story.append(p(field["value"] + " - " + source(field)))
    else:
        story.append(p("Instrument serials not confirmed in reviewed records."))
    story.append(p("C. Source readings and transcription status", "CertH2"))
    listed = set()
    for kind, section in by_type.items():
        if section.get("fields"):
            story.append(p(section.get("title") or kind.replace("_", " ").title(), "CertH2"))
            story.extend(reading_tables(section))
            listed.update(field.get("id") for field in section["fields"])
    additional = [field for field in data.get("fields", []) if field.get("id") not in listed]
    if additional:
        story.append(p("Other source readings", "CertH2"))
        story.extend(reading_tables({"fields": additional}))
    story.append(p("D. Rule provenance", "CertH2"))
    if verdicts:
        provenance = [["Engineering check", "Source clause / basis", "Rule status"]]
        for item in verdicts:
            provenance.append(
                [
                    item.get("title") or "Engineering check",
                    item.get("source_clause") or "Not confirmed",
                    item.get("rule_status") or "Not confirmed",
                ]
            )
        story.append(grid(provenance, [170, 255, 80]))
    else:
        story.append(p("No engineering rules assigned."))
    review_counts = {
        name: sum(field.get("status") == name for field in data.get("fields", []))
        for name in ("verified", "unreviewed", "ambiguous", "not_applicable")
    }
    story += [
        p("E. Data-review summary", "CertH2"),
        p(", ".join(f"{label}: {review_counts[label]}" for label in review_counts)),
    ]
    normalizations = data.get("provisional_normalizations") or []
    if normalizations:
        story.append(
            p(
                "Preview-only interpretations of source text; these do not alter stored readings.",
                "CertSmall",
            )
        )
        story.append(
            grid(
                [["Source field", "Original", "Preview interpretation"]]
                + [
                    [
                        entry.get("label", ""),
                        f"{entry.get('original','')} {entry.get('original_unit','')}",
                        f"{entry.get('preview','')} {entry.get('preview_unit','')}",
                    ]
                    for entry in normalizations
                ],
                [215, 135, 155],
            )
        )
    story += [
        p("F. Abbreviations", "CertH2"),
        grid(
            [
                ["BT", "Before test"],
                ["AT", "After test"],
                ["HV", "High voltage"],
                ["LV", "Low voltage"],
                ["IR", "Insulation resistance"],
                ["RMS", "Root mean square"],
            ],
            [95, 410],
            header=False,
        ),
        Spacer(1, 20),
        p("- End of Report -", "CertH1"),
    ]
    stream = BytesIO()
    doc = SimpleDocTemplate(
        stream,
        pagesize=(595, 842),
        leftMargin=45,
        rightMargin=45,
        topMargin=60,
        bottomMargin=62,
        title=f"Test certificate {certificate}",
        author="Operation Vector",
    )
    doc.build(
        story,
        canvasmaker=lambda *args, **kwargs: NumberedCanvas(
            *args,
            certificate=certificate,
            sample=sample,
            revision=report.revision,
            provisional=provisional,
            **kwargs,
        ),
    )
    return stream.getvalue()
