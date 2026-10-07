"""Provision a privileged user's authenticator from a trusted server terminal."""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django_otp.plugins.otp_totp.models import TOTPDevice

from lab.roles import ADMIN, HOD, QUALITY, role_for


class Command(BaseCommand):
    help = "Provision a TOTP device; output contains a secret and must be handed to its owner privately."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(username=options["username"], is_active=True).first()
        if not user or role_for(user) not in (ADMIN, HOD, QUALITY):
            raise CommandError("Active HoD, Quality, or Admin account required.")
        if TOTPDevice.objects.filter(user=user, confirmed=True).exists():
            raise CommandError(
                "A confirmed authenticator already exists; use a controlled reset procedure."
            )
        device = TOTPDevice.objects.create(user=user, name="CPRI authenticator", confirmed=True)
        self.stdout.write("Give this URI directly to the account owner. Treat it as a secret:")
        self.stdout.write(device.config_url)
