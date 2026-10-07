from django.core.management.base import BaseCommand

from lab.models import ReportTemplate
from lab.template_mapping import default_definition


class Command(BaseCommand):
    help = "Create the editable laboratory summary template without replacing existing versions."

    def handle(self, *args, **kwargs):
        obj, created = ReportTemplate.objects.get_or_create(
            name="Laboratory summary", version=2, defaults={"definition": default_definition()}
        )
        obj.full_clean()
        self.stdout.write("Created " + str(obj) if created else "Preserved " + str(obj))
