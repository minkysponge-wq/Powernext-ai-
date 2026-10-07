"""Install provisional, selectable transformer verdict rules; never attach automatically."""

from django.core.management.base import BaseCommand, CommandError

from lab.models import Rule

SOURCE = "Team-relayed organiser clarification, 29 September 2026; exact source clause and applicability require CPRI confirmation."


def field(form_type, schema_key):
    return {"form_type": form_type, "schema_key": schema_key}


def derived(stage, metric):
    return {"derived": {"form_type": "loss_calculation", "stage": stage, "metric": metric}}


def candidates():
    for stage in ("NTBT", "NTAT", "HTBT", "HTAT", "LTBT", "LTAT"):
        for load in (("50", "100") if stage in ("NTBT", "NTAT") else ("100",)):
            yield (
                f"LOSS_{load}_{stage}",
                f"Total loss at {load}% load · {stage}",
                "identity",
                {
                    "test_type": "loss_calculation",
                    "inputs": [
                        derived(stage, "total_loss_" + load),
                        derived(stage, "limit_" + load),
                    ],
                    "unit": "W",
                    "upper_input": 1,
                    "tolerance": 0,
                    "marginal_percent": 2,
                },
            )
        yield (
            f"IMPEDANCE_{stage}",
            f"Impedance at 75 °C · {stage}",
            "percent_deviation",
            {
                "test_type": "loss_calculation",
                "inputs": [derived(stage, "Z75"), field("transformer_proforma", "impedance_at_75")],
                "unit": "%",
                "lower": -10,
                "upper": 10,
                "tolerance": 0,
                "marginal_percent": 2,
            },
        )
    for before, after in (("NTBT", "NTAT"), ("HTBT", "HTAT"), ("LTBT", "LTAT")):
        yield (
            f"REACTANCE_{before}_{after}",
            f"Reactance change · {before} to {after}",
            "absolute_percent_change",
            {
                "test_type": "short_circuit",
                "inputs": [derived(before, "X50"), derived(after, "X50")],
                "unit": "%",
                "upper": 2,
                "tolerance": 0,
                "marginal_percent": 2,
                "conditions": [
                    {
                        "selector": field("transformer_proforma", "circular"),
                        "unit": "",
                        "accepted_values": ["yes", "true", "1", "checked"],
                        "excluded_values": ["no", "false", "0", "not checked"],
                    }
                ],
            },
        )
    for label, suffix in (("100%", "100"), ("112.5%", "112")):
        yield (
            f"NO_LOAD_CURRENT_{suffix}",
            f"No-load current at {label} voltage",
            "identity",
            {
                "test_type": "loss_measurement",
                "inputs": [
                    field("loss_measurement", f"no_load_{suffix}_percent"),
                    field("loss_measurement", f"no_load_{suffix}_limit_percent"),
                ],
                "unit": "%",
                "upper_input": 1,
                "tolerance": 0,
                "marginal_percent": 2,
            },
        )
    for code, key, guarantee in (
        ("TOP_OIL_RISE", "top_oil_rise", "guaranteed_temp_rise_1"),
        ("HV_WINDING_RISE", "hv_winding_rise", "guaranteed_temp_rise_2"),
        ("LV_WINDING_RISE", "lv_winding_rise", "guaranteed_temp_rise_2"),
    ):
        yield (
            code,
            key.replace("_", " ").title(),
            "identity",
            {
                "test_type": "temperature_rise",
                "inputs": [
                    field("temperature_rise", key),
                    field("transformer_proforma", guarantee),
                ],
                "unit": "K",
                "upper_input": 1,
                "tolerance": 0,
                "marginal_percent": 2,
            },
        )
    yield (
        "INDUCED_DIELECTRIC",
        "Induced dielectric withstand",
        "all_text",
        {
            "test_type": "routine_test",
            "fields": [],
            "inputs": [
                field("routine_test", "induced_observation_BT"),
                field("routine_test", "induced_observation_AT"),
            ],
            "unit": "",
            "accepted_values": ["Withstood", "No breakdown", "Satisfactory"],
        },
    )


class Command(BaseCommand):
    help = "Install provisional transformer verdict candidates for runtime job selection; does not attach them to jobs."

    def handle(self, *args, **options):
        created = 0
        for code, title, operation, parameters in candidates():
            defaults = {
                "title": title,
                "operation": operation,
                "parameters": parameters,
                "source_clause": SOURCE,
                "status": "assumed",
            }
            rule, was_created = Rule.objects.get_or_create(code=code, version=1, defaults=defaults)
            if not was_created and any(
                getattr(rule, key) != value for key, value in defaults.items()
            ):
                raise CommandError(
                    f"{code} v1 differs from the installed version. Add a new version; do not overwrite evidence."
                )
            created += was_created
        self.stdout.write(
            self.style.SUCCESS(f"{created} provisional rules installed; none assigned to jobs.")
        )
