"""Add cited limits to the still-pending v2 configuration without changing v1."""

from copy import deepcopy

from django.core.management.base import BaseCommand, CommandError

from lab.management.commands.seed_verdict_v2 import v2_definitions
from lab.models import Rule


class Command(BaseCommand):
    help = "Add reviewed, cited limit parameters to four pending v2 rules."

    def handle(self, *args, **options):
        changes = {
            "VOLTAGE_RATIO": ("ratio_max_percent", "ratio_impedance_fraction", "ratio_basis"),
            "TOP_OIL_RISE": ("standard_limits", "standard_limits_source"),
            "HV_WINDING_RISE": ("standard_limits", "standard_limits_source"),
            "LV_WINDING_RISE": ("standard_limits", "standard_limits_source"),
        }
        definitions = {
            item["code"]: item
            for item in v2_definitions(Rule.objects.filter(version=1).order_by("code"))
        }
        changed = 0
        for code, keys in changes.items():
            current = Rule.objects.get(code=code, version=2)
            if current.status != "assumed":
                raise CommandError(
                    f"{code}: only pending, unadopted rules can receive this draft correction."
                )
            expected = definitions[code]["parameters"]
            old = deepcopy(current.parameters)
            for key in keys:
                if key in old and old[key] != expected[key]:
                    raise CommandError(f"{code}: existing {key} differs; manual review required.")
                old[key] = expected[key]
            if old != current.parameters:
                current.parameters = old
                current.full_clean()
                current.save(update_fields=["parameters"])
                changed += 1
        self.stdout.write(
            self.style.SUCCESS(f"{changed} pending v2 rule configurations refreshed; v1 unchanged.")
        )
