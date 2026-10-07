import json

from django.core.management.base import BaseCommand

from lab.fixed_template import TEMPLATE_NAME, default_fixed_definition, validate_fixed_definition
from lab.models import ReportTemplate


class Command(BaseCommand):
    help = "Install the immutable CPRI-SCL-TR-v1 report layout and bindings."

    def handle(self, *args, **options):
        definition = default_fixed_definition()
        validate_fixed_definition(definition)
        definition = json.loads(json.dumps(definition))
        template, created = ReportTemplate.objects.get_or_create(
            name=TEMPLATE_NAME, version=8, defaults={"definition": definition, "active": True}
        )
        ReportTemplate.objects.filter(name=TEMPLATE_NAME).exclude(pk=template.pk).update(
            active=False
        )
        if not created and template.definition != definition:
            self.stdout.write(
                self.style.WARNING(
                    "Version 8 already exists with a different definition; kept unchanged."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"{template} ready"))
