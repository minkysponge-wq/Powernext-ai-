"""Create an immutable team-reviewed v2 rule set; v1 and frozen reports remain intact."""

from copy import deepcopy

from django.core.management.base import BaseCommand, CommandError

from lab.models import Rule

REVIEW = "reviewed by team, pending CPRI adoption"
STANDARDS = {
    "eel": "IS 1180 (Part 1):2014 incl. Amendment 4",
    "impedance": "IS 2026 (Part 1):2011",
    "temperature": "IS 2026 (Part 2):2010 method; limits from IS 1180 (Part 1)",
    "dielectric": "IS 2026 (Part 3):2018",
    "short_circuit": "IS 2026 (Part 5):2011",
    "no_load": "IS 2026 (Part 1):2011; IS 1180 (Part 1)",
}
EEL_TABLE = {"250": {"EEL-1": {"50": 980, "100": 2930}}}


def selector(form_type, key):
    return {"form_type": form_type, "schema_key": key}


def v2_definitions(v1_rules):
    for old in v1_rules:
        code = old.code
        p = deepcopy(old.parameters)
        p["decision_status"] = REVIEW
        p["quality_margin_percent"] = p.pop("marginal_percent", 2)
        if code.startswith("LOSS_"):
            load, stage = code.split("_")[1:]
            p.update(stage=stage, load=load)
            if stage in ("NTBT",):
                p.update(v2_kind="eel_principal", v2_status="ACTIVE", eel_table=EEL_TABLE)
                reason = "Use amended EEL table by kVA/EEL and block worksheet limit mismatch."
            elif stage == "NTAT":
                p.update(
                    v2_kind="descriptive", v2_status="DESCRIPTIVE", metric="total_loss_" + load
                )
                reason = "After-test loss is a BT/AT comparison, without pass/fail."
            else:
                p.update(
                    v2_kind="not_applicable",
                    v2_status="N/A",
                    reason="EEL applies at the principal tap only.",
                )
                reason = p["reason"]
            standard = STANDARDS["eel"]
        elif code.startswith("IMPEDANCE_"):
            stage = code.split("_")[1]
            p["stage"] = stage
            if stage.endswith("AT"):
                p.update(v2_kind="descriptive", v2_status="DESCRIPTIVE", metric="Z75")
                reason = "After-test impedance is a BT/AT comparison, without pass/fail."
            else:
                p.update(v2_kind="impedance", v2_status="ACTIVE")
                reason = (
                    "Principal tap: measured %Z against ±10% band around declaration."
                    if stage == "NTBT"
                    else "Non-principal tap: ±15% only with a tap-specific declaration; otherwise N/A."
                )
            standard = STANDARDS["impedance"]
        elif code.startswith("REACTANCE_"):
            before, after = code[len("REACTANCE_") :].split("_")
            p.update(v2_kind="reactance", v2_status="ACTIVE", before=before, after=after)
            reason = (
                "Signed BT−AT change from 3 dp X; absolute change ≤2% for confirmed circular coil."
            )
            standard = STANDARDS["short_circuit"]
        elif code.startswith("NO_LOAD_CURRENT_"):
            voltage = code.rsplit("_", 1)[-1]
            p.update(
                v2_kind="no_load",
                v2_status="ACTIVE",
                voltage=voltage,
                upper=2 if voltage == "100" else 5,
            )
            reason = (
                "2% of full-load current at rated voltage."
                if voltage == "100"
                else "5% special-test limit, only when requested in CRF."
            )
            standard = STANDARDS["no_load"]
        elif code in ("TOP_OIL_RISE", "HV_WINDING_RISE", "LV_WINDING_RISE"):
            p.update(
                v2_kind="temperature",
                v2_status="ACTIVE",
                key={
                    "TOP_OIL_RISE": "top_oil_rise",
                    "HV_WINDING_RISE": "hv_winding_rise",
                    "LV_WINDING_RISE": "lv_winding_rise",
                }[code],
                guarantee=(
                    "guaranteed_temp_rise_1" if code == "TOP_OIL_RISE" else "guaranteed_temp_rise_2"
                ),
                standard_limits={
                    "up_to_200": {"top_oil": 35, "winding": 40},
                    "250_to_2500": {"top_oil": 40, "winding": 45},
                },
                standard_limits_source=STANDARDS["eel"],
            )
            reason = "Use the stricter of the declared guarantee and applicable IS 1180-1 limit."
            standard = STANDARDS["temperature"]
        elif code == "INDUCED_DIELECTRIC":
            p.update(
                v2_kind="observation",
                v2_status="OBSERVATION",
                observation_fields=[
                    ["routine_test", "induced_observation_BT"],
                    ["routine_test", "induced_observation_AT"],
                ],
            )
            reason = (
                "Normalised BT/AT observation; unrecognised wording requires engineer confirmation."
            )
            standard = STANDARDS["dielectric"]
        else:
            raise CommandError("Unmapped v1 rule: " + code)
        p["v2_change_reason"] = reason
        source = f"{standard}; clause TBC; {REVIEW}"
        yield {
            "code": code,
            "title": old.title,
            "operation": old.operation,
            "parameters": p,
            "source_clause": source,
            "status": "assumed",
        }

    additions = [
        (
            "VOLTAGE_RATIO",
            "Principal-tap voltage ratio",
            "percent_deviation",
            "ratio",
            "ACTIVE",
            "Lower of ±0.5% and one tenth of measured %Z; declared principal ratio required.",
            "impedance",
            [selector("transformer_proforma", "declared_ratio_principal")],
        ),
        (
            "SEPARATE_SOURCE_AC",
            "Separate-source AC withstand",
            "all_text",
            "observation",
            "OBSERVATION",
            "Normalised before/after HV and LV observations.",
            "dielectric",
            [
                selector("routine_test", "hv_observation_BT"),
                selector("routine_test", "lv_observation_BT"),
                selector("routine_test", "hv_observation_AT"),
                selector("routine_test", "lv_observation_AT"),
            ],
        ),
        (
            "PRESSURE_TEST",
            "Pressure / vacuum observation",
            "all_text",
            "pressure_observation",
            "OBSERVATION",
            "Logsheet observation with explicit kPa normalisation; unknown text blocked.",
            "pressure",
            [selector("pressure_oil_leakage", "routine_pressure_observation")],
        ),
        (
            "OIL_LEAKAGE",
            "Oil leakage observation",
            "all_text",
            "oil_observation",
            "OBSERVATION",
            "Logsheet oil observation; unknown text blocked.",
            "pressure",
            [selector("pressure_oil_leakage", "oil_observation")],
        ),
        (
            "SC_OVERALL",
            "Overall short-circuit withstand",
            "identity",
            "sc_overall",
            "ACTIVE",
            "Engineer confirmation requires R15–R17, repeated routine tests and no visible defects.",
            "short_circuit",
            [selector("short_circuit", "overall_engineer_verdict")],
        ),
    ]
    for code, title, operation, kind, status, reason, family, inputs in additions:
        p = {
            "v2_kind": kind,
            "v2_status": status,
            "v2_change_reason": reason,
            "decision_status": REVIEW,
            "quality_margin_percent": 2,
            "test_type": {
                "VOLTAGE_RATIO": "routine_test",
                "SEPARATE_SOURCE_AC": "routine_test",
                "PRESSURE_TEST": "pressure_oil_leakage",
                "OIL_LEAKAGE": "pressure_oil_leakage",
                "SC_OVERALL": "short_circuit",
            }[code],
            "unit": "",
            "inputs": inputs,
        }
        if kind == "ratio":
            p.update(
                ratio_max_percent="0.5",
                ratio_impedance_fraction="0.1",
                ratio_basis="Nameplate HV/LV voltages and Dyn vector group; [CLAUSE TBC]",
            )
        if operation == "all_text":
            p["accepted_values"] = [
                "Withstood",
                "No disruptive discharge, withstood",
                "No leakage observed",
            ]
        if kind in ("observation", "pressure_observation", "oil_observation"):
            p["observation_fields"] = [[item["form_type"], item["schema_key"]] for item in inputs]
        yield {
            "code": code,
            "title": title,
            "operation": operation,
            "parameters": p,
            "source_clause": (
                f"{STANDARDS[family]}; clause TBC; {REVIEW}"
                if family in STANDARDS
                else f"clause TBC; {REVIEW}"
            ),
            "status": "assumed",
        }


class Command(BaseCommand):
    help = "Seed 23 immutable v2 replacements plus five new v2 rules; v1 is never modified."

    def handle(self, *args, **options):
        old = list(Rule.objects.filter(version=1).order_by("code"))
        if len(old) != 23:
            raise CommandError(f"Expected 23 v1 rules; found {len(old)}.")
        definitions = list(v2_definitions(old))
        if len(definitions) != 28 or len({item["code"] for item in definitions}) != 28:
            raise CommandError("V2 definition set is incomplete or has duplicate codes.")
        created = 0
        for definition in definitions:
            code = definition.pop("code")
            current, fresh = Rule.objects.get_or_create(code=code, version=2, defaults=definition)
            if not fresh and any(
                getattr(current, key) != value for key, value in definition.items()
            ):
                raise CommandError(
                    f"{code} v2 already differs; create a new version instead of editing history."
                )
            if fresh:
                current.full_clean()
                created += 1
        self.stdout.write(self.style.SUCCESS(f"{created} v2 rules created; 23 v1 rules retained."))
