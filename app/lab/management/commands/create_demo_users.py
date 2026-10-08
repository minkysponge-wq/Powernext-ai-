"""Create isolated synthetic walkthrough accounts with one-time MFA enrollment."""

import json
import secrets
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_otp.plugins.otp_totp.models import TOTPDevice

from lab.roles import ADMIN, CUSTOMER, ENGINEER, HOD, QUALITY

DEMO_ROLES = {
    "customer": ("walkthrough_customer", CUSTOMER, None),
    "admin": ("walkthrough_admin", ADMIN, None),
    "engineer_a": ("walkthrough_engineer_a", ENGINEER, "lock_report"),
    "engineer_b": ("walkthrough_engineer_b", ENGINEER, "lock_report"),
    "quality": ("walkthrough_quality", QUALITY, "verify_report"),
    "hod": ("walkthrough_hod", HOD, "approve_report"),
}

DEMO_DISPLAY_NAMES = {
    "customer": "Demo Customer",
    "admin": "Demo Administrator",
    "engineer_a": "Maneesh",
    "engineer_b": "Station B",
    "quality": "Suhas",
    "hod": "Sampreeth",
}


class Command(BaseCommand):
    help = "Create local synthetic users and print each authenticator enrollment URI once."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo account creation is local-only (DJANGO_DEBUG=1).")
        access = Path(settings.BASE_DIR).parent / "output" / "demo-users.json"
        if (
            access.exists()
            or get_user_model()
            .objects.filter(username__in=[row[0] for row in DEMO_ROLES.values()])
            .exists()
        ):
            raise CommandError("Demo accounts already exist; refusing to reset credentials or MFA.")
        password = secrets.token_urlsafe(24)
        users = {}
        enrollment = {}
        with transaction.atomic():
            for label, (username, role, permission) in DEMO_ROLES.items():
                user = get_user_model().objects.create_user(username=username, password=password)
                names = DEMO_DISPLAY_NAMES[label].split(" ", 1)
                user.first_name = names[0]
                user.last_name = names[1] if len(names) > 1 else ""
                user.save(update_fields=["first_name", "last_name"])
                user.groups.add(Group.objects.get(name=role))
                if permission:
                    user.user_permissions.add(Permission.objects.get(codename=permission))
                users[label] = username
                if role in (ADMIN, ENGINEER, QUALITY, HOD):
                    device = TOTPDevice.objects.create(
                        user=user, name="VectorLab DEMO authenticator", confirmed=True
                    )
                    enrollment[label] = device.config_url
        access.parent.mkdir(parents=True, exist_ok=True)
        access.write_text(
            json.dumps(
                {"password": password, "users": users, "totp_enrollment": enrollment}, indent=2
            ),
            encoding="utf-8",
        )
        self.stdout.write("Synthetic accounts created. Hand each URI to its demo role owner once:")
        self.stdout.write(f"Temporary password: {password}")
        for label, uri in enrollment.items():
            self.stdout.write(f"{label}: {uri}")
        self.stdout.write(f"Ignored local access file: {access}")
