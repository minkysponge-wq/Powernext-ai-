"""Seed one fictional fixed-template job; never perform an approval stage."""

import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice

from lab.fixed_template import TESTS
from lab.management.commands.create_demo_users import DEMO_ROLES
from lab.models import AuditEvent, Job, Report, ReportTemplate, Rule, TestRun
from lab.synthetic_demo import attach_demo_rules, create_fields

STATIONS = (
    "routine_test",
    "short_circuit",
    "loss_measurement",
    "loss_calculation",
    "transformer_proforma",
    "temperature_rise",
    "pressure_oil_leakage",
)


class Command(BaseCommand):
    help = "Seed one synthetic 18-section job with station evidence; never approve it."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("The synthetic walkthrough is local-only.")
        root = Path(settings.BASE_DIR).parent
        access_file = root / "output" / "walkthrough-demo-access.json"
        if access_file.exists():
            self.stdout.write(access_file.read_text(encoding="utf-8"))
            return
        if Job.objects.filter(customer__startswith="SYNTHETIC WALKTHROUGH").exists():
            raise CommandError("Synthetic job exists but access file is missing; inspect it first.")
        template = ReportTemplate.objects.get(name="CPRI-SCL-TR-v1", version=8)
        if not Rule.objects.filter(version=1).exists():
            call_command("seed_verdict_candidates", stdout=self.stdout)
        if not Rule.objects.filter(version=2).exists():
            call_command("seed_verdict_v2", stdout=self.stdout)
        users_file = root / "output" / "demo-users.json"
        if not users_file.exists():
            call_command("create_demo_users", stdout=self.stdout)
        access = json.loads(users_file.read_text(encoding="utf-8"))
        password = access["password"]
        self.clients = {}
        with transaction.atomic():
            users = {
                name: get_user_model().objects.get(username=details[0])
                for name, details in DEMO_ROLES.items()
            }
            customer = users["customer"]
            admin = users["admin"]
            requested = list(STATIONS)
            self.post(
                customer,
                "customer_new_request",
                data={
                    "customer": "SYNTHETIC WALKTHROUGH — Example Power Systems",
                    "customer_address": "Demo-only address, Bengaluru",
                    "manufacturer": "SYNTHETIC DEMO Manufacturer",
                    "sample_particulars": "Fictional 250 kVA 11 kV/433 V Dyn11 transformer",
                    "requested_tests": requested,
                },
            )
            job = Job.objects.filter(customer_user=customer).order_by("-created_at").first()
            job.report_template = template
            job.save(update_fields=["report_template"])
            self.post(
                admin,
                "job_scope",
                [job.pk],
                {
                    "customer": job.customer,
                    "sample_code": "SYN-FULL-001",
                    "test_series": "SYN-SERIES-001",
                    "report_scope": requested,
                    "report_test_ids": [item[0] for item in TESTS],
                    "scope_note": "Fictional full-template demo; no CPRI conformity claim.",
                    "version": job.version,
                },
            )
            job.refresh_from_db()
            station_actors = {
                kind: users["engineer_b"] if index % 2 else users["engineer_a"]
                for index, kind in enumerate(STATIONS)
            }
            count = create_fields(job, station_actors, template.definition)
            rule_count = attach_demo_rules(job)
            for index, kind in enumerate(STATIONS):
                assigned = users["engineer_b"] if index % 2 else users["engineer_a"]
                self.post(
                    admin,
                    "assign_test",
                    [job.pk],
                    {
                        "test_type": kind,
                        "station": f"Synthetic bay {index + 1}",
                        "assigned_to": assigned.pk,
                    },
                )
                run = TestRun.objects.get(job=job, test_type=kind)
                self.post(assigned, "start_test", [run.pk])
            AuditEvent.objects.create(
                job=job,
                actor=admin,
                action="synthetic_walkthrough_seeded",
                details={
                    "fields": count,
                    "candidate_v2_rules": rule_count,
                    "no_real_source": True,
                    "no_approval": True,
                },
            )
            if Report.objects.filter(job=job).exists():
                raise CommandError("The seed unexpectedly created a report.")
            data = {
                "notice": "LOCAL SYNTHETIC DATA ONLY. No report is approved by this seed.",
                "url": "http://127.0.0.1:8000/accounts/login/",
                "password": password,
                "users": {name: user.username for name, user in users.items()},
                "job": {
                    "id": str(job.pk),
                    "file_number": job.file_number,
                    "customer_url": f"http://127.0.0.1:8000/customer/requests/{job.file_number}/",
                    "lab_url": f"http://127.0.0.1:8000/jobs/{job.pk}/",
                },
            }
        access_file.parent.mkdir(parents=True, exist_ok=True)
        access_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.stdout.write(f"Synthetic job ready: {job.pk}; access details in {access_file}")

    def post(self, user, name, args=None, data=None):
        client = self.clients.get(user.pk)
        if client is None:
            client = Client(SERVER_NAME="127.0.0.1")
            client.force_login(user)
            if settings.MFA_ENFORCED and TOTPDevice.objects.filter(user=user, confirmed=True).exists():
                device = TOTPDevice.objects.get(user=user, confirmed=True)
                response = client.post(
                    reverse("otp_verify"), {"token": str(totp(device.bin_key)).zfill(6)}
                )
                if response.status_code != 302 or response["Location"].startswith(
                    reverse("otp_verify")
                ):
                    raise CommandError(f"Demo MFA enrollment failed for {user.username}.")
            self.clients[user.pk] = client
        response = client.post(reverse(name, args=args or []), data or {})
        if response.status_code != 302 or response["Location"].startswith(reverse("otp_verify")):
            detail = response.content.decode("utf-8", errors="replace")[:350]
            raise CommandError(
                f"{user.username}: {name} returned {response.status_code}"
                f" ({response.get('Location', '')}): {detail}"
            )
        return response
