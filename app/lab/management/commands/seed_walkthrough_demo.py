"""Create a local-only, synthetic multi-role product walkthrough."""

import json
import secrets
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test import Client
from django.urls import reverse

from lab.models import AuditEvent, Job, Report, TestRun
from lab.roles import ADMIN, CUSTOMER, ENGINEER, HOD, QUALITY

PREFIX = "walkthrough_"


class Command(BaseCommand):
    help = "Seed synthetic, local-only jobs at station, Quality, HoD and issued stages."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("The walkthrough seed is disabled outside local DEBUG mode.")
        output = Path(settings.BASE_DIR).parent / "output" / "walkthrough-demo-access.json"
        if output.exists():
            data = json.loads(output.read_text(encoding="utf-8"))
            self.stdout.write(json.dumps(data, indent=2))
            return
        if Job.objects.filter(customer__startswith="SYNTHETIC WALKTHROUGH").exists():
            raise CommandError(
                "Walkthrough jobs exist but the access file is missing; inspect them before reseeding."
            )
        password = secrets.token_urlsafe(15)
        roles = {
            "customer": (PREFIX + "customer", CUSTOMER),
            "admin": (PREFIX + "admin", ADMIN),
            "engineer_a": (PREFIX + "engineer_a", ENGINEER),
            "engineer_b": (PREFIX + "engineer_b", ENGINEER),
            "quality": (PREFIX + "quality", QUALITY),
            "hod": (PREFIX + "hod", HOD),
        }
        users = {}
        with transaction.atomic():
            for key, (username, group_name) in roles.items():
                user, created = get_user_model().objects.get_or_create(username=username)
                if not created and user.groups.exclude(name=group_name).exists():
                    raise CommandError(
                        f"Existing account {username} has another role; refusing to change it."
                    )
                user.set_password(password)
                user.is_active = True
                user.save(update_fields=["password", "is_active"])
                user.groups.set([Group.objects.get(name=group_name)])
                users[key] = user
            jobs = {}
            for stage in ("station", "quality", "hod", "issued"):
                jobs[stage] = self.make_case(stage, users)
            data = {
                "notice": "LOCAL SYNTHETIC DATA ONLY. Never use these accounts for real records.",
                "url": "http://127.0.0.1:8000/accounts/login/",
                "password": password,
                "users": {key: user.username for key, user in users.items()},
                "jobs": {
                    stage: {
                        "file_number": job.file_number,
                        "id": str(job.pk),
                        "customer_url": f"http://127.0.0.1:8000/customer/requests/{job.file_number}/",
                        "lab_url": f"http://127.0.0.1:8000/jobs/{job.pk}/",
                    }
                    for stage, job in jobs.items()
                },
            }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.stdout.write(json.dumps(data, indent=2))

    def post(self, user, name, args=None, data=None, query=""):
        client = Client(SERVER_NAME="127.0.0.1")
        client.force_login(user)
        url = reverse(name, args=args or []) + query
        response = client.post(url, data or {})
        if response.status_code != 302:
            detail = response.content.decode("utf-8", errors="replace")[:350]
            raise CommandError(f"{user.username}: {name} returned {response.status_code}: {detail}")
        return response

    def make_case(self, stage, users):
        customer = users["customer"]
        admin = users["admin"]
        engineer = users["engineer_a"]
        types = ["pressure_oil_leakage"]
        if stage == "issued":
            types.append("short_circuit")
        self.post(
            customer,
            "customer_new_request",
            data={
                "customer": "SYNTHETIC WALKTHROUGH — Example Power Systems",
                "customer_address": "Demo-only address, Bengaluru",
                "manufacturer": "SYNTHETIC DEMO Manufacturer",
                "sample_particulars": f"SYNTHETIC {stage.upper()} sample; no real laboratory result",
                "requested_tests": types,
            },
        )
        job = Job.objects.filter(customer_user=customer).order_by("-created_at").first()
        self.post(
            admin,
            "job_scope",
            [job.pk],
            {
                "customer": job.customer,
                "sample_code": f"SYN-{stage.upper()}-001",
                "test_series": f"DEMO-{stage.upper()}",
                "report_scope": types,
                "scope_note": "Synthetic product walkthrough. Only selected demo tests apply; no CPRI conformity claim.",
                "version": job.version,
            },
        )
        job.refresh_from_db()
        for index, kind in enumerate(types):
            assigned = users["engineer_b"] if index else engineer
            self.post(
                admin,
                "assign_test",
                [job.pk],
                {"test_type": kind, "station": f"Demo bay {index+1}", "assigned_to": assigned.pk},
            )
            run = TestRun.objects.get(job=job, test_type=kind)
            self.post(assigned, "start_test", [run.pk])
            if stage != "station":
                values = {
                    "series": job.test_series,
                    "sample_code": job.sample_code,
                    "customer": job.customer,
                    "oil_observation": "No leakage observed (synthetic)",
                    "shots.0.peak": "12.3",
                }
                self.enter_minimum(job, run, assigned, values)
                self.post(
                    assigned,
                    "mark_unused_not_applicable",
                    [run.pk],
                    {
                        "confirmed": "yes",
                        "reason": "Synthetic walkthrough: other optional measurements do not apply to this demo case.",
                    },
                )
                self.post(assigned, "lock_test", [run.pk], {"confirmed": "yes"})
        if stage != "station":
            report = Report.objects.filter(job=job).order_by("-revision").first()
            if not report:
                raise CommandError(f"{stage}: last station lock did not generate a draft")
            if report.snapshot.get("blockers"):
                ids = [
                    f["id"]
                    for f in report.snapshot.get("findings", [])
                    if f.get("severity") == "blocker"
                ]
                raise CommandError(
                    f'{stage}: report has {report.snapshot["blockers"]} blockers: {ids[:20]}'
                )
            self.post(engineer, "engineer_lock", [report.pk], {"confirmed": "yes"})
            if stage in ("hod", "issued"):
                self.post(users["quality"], "quality_verify", [report.pk], {"confirmed": "yes"})
            if stage == "issued":
                self.post(users["hod"], "approve_report", [report.pk], {"confirmed": "yes"})
        AuditEvent.objects.create(
            job=job,
            actor=admin,
            action="synthetic_walkthrough_case",
            details={"stage": stage, "not_real_lab_evidence": True},
        )
        return job

    def enter_minimum(self, job, run, user, values):
        from django.core.paginator import Paginator

        from lab.quality import identity

        fields = job.fields.filter(
            document__isnull=True, context__form_type=run.test_type
        ).order_by("key")
        for page in Paginator(fields, 30):
            rows = list(page.object_list)
            payload = {
                "form-TOTAL_FORMS": len(rows),
                "form-INITIAL_FORMS": len(rows),
                "form-MIN_NUM_FORMS": 0,
                "form-MAX_NUM_FORMS": 1000,
                "action": "save",
            }
            for index, field in enumerate(rows):
                key = identity(field)[2]
                chosen = key in values and (
                    key != "customer" or run.test_type == "pressure_oil_leakage"
                )
                value = values[key] if chosen else field.value
                payload.update(
                    {
                        f"form-{index}-id": field.pk,
                        f"form-{index}-value": value,
                        f"form-{index}-unit": "kA" if key == "shots.0.peak" else field.unit,
                        f"form-{index}-status": "verified" if chosen else field.status,
                        f"form-{index}-version": field.version,
                    }
                )
            self.post(
                user,
                "digital_review",
                [job.pk],
                payload,
                query=f"?test={run.test_type}&page={page.number}",
            )
