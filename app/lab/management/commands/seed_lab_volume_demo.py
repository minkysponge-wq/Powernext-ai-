"""Add ten clearly synthetic cases using the existing tested walkthrough flow."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from lab.management.commands.seed_walkthrough_demo import Command as Walkthrough
from lab.models import AuditEvent

TARGETS = {"station": 3, "quality": 3, "hod": 2, "issued": 2}
USER_NAMES = ("customer", "admin", "engineer_a", "engineer_b", "quality", "hod")


class Command(BaseCommand):
    help = "Add 10 synthetic jobs across workflow stages; local DEBUG only and idempotent."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Synthetic volume cases are disabled outside local DEBUG mode.")
        users = {}
        for role in USER_NAMES:
            user = get_user_model().objects.filter(username="walkthrough_" + role).first()
            if (
                not user
                or not user.groups.filter(
                    name={
                        "customer": "Customer",
                        "admin": "Admin",
                        "engineer_a": "Test Engineer",
                        "engineer_b": "Test Engineer",
                        "quality": "Quality",
                        "hod": "HoD",
                    }[role]
                ).exists()
            ):
                raise CommandError(
                    "Run seed_walkthrough_demo first; expected demo roles are missing."
                )
            users[role] = user
        builder = Walkthrough()
        created = 0
        for stage, target in TARGETS.items():
            existing = AuditEvent.objects.filter(
                action="synthetic_volume_seed", details__stage=stage
            ).count()
            for _ in range(max(0, target - existing)):
                job = builder.make_case(stage, users)
                AuditEvent.objects.create(
                    job=job,
                    actor=users["admin"],
                    action="synthetic_volume_seed",
                    details={"stage": stage, "not_real_lab_evidence": True},
                )
                created += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"{created} synthetic volume jobs added; target is 10 across station, Quality, HoD and issued."
            )
        )
