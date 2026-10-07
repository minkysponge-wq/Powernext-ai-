from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Document, Field, Job, Report, TestRun
from .roles import ADMIN, CUSTOMER, ENGINEER, HOD, QUALITY, lab_access, role_for


class RoleBoundaryTests(TestCase):
    def test_unrelated_accounts_cannot_fetch_private_evidence_by_id(self):
        owner = get_user_model().objects.create_user("evidence-owner")
        owner.groups.add(Group.objects.get(name=ENGINEER))
        job = Job.objects.create(owner=owner, title="Private evidence", customer="Fixture")
        document = Document.objects.create(
            job=job,
            original_name="source.pdf",
            file="sources/fixture.pdf",
            sha256="a" * 64,
            page_count=1,
            status="ready",
        )
        field = Field.objects.create(
            job=job,
            key="source-reading",
            label="Reading",
            value="12",
            raw_value="12",
            origin="scan",
            document=document,
            page=1,
            bbox=[0.1, 0.1, 0.2, 0.2],
            updated_by=owner,
        )
        report = Report.objects.create(
            job=job, revision=1, snapshot={"fields": []}, snapshot_sha256="0" * 64, created_by=owner
        )
        paths = [
            reverse("job_detail", args=[job.pk]),
            reverse("source", args=[document.pk]),
            reverse("source_page", args=[document.pk, 1]),
            reverse("field_crop", args=[field.pk]),
            reverse("report", args=[report.pk]),
            reverse("report_pdf", args=[report.pk]),
            reverse("report_evidence", args=[report.pk]),
            reverse("audit_history", args=[job.pk]),
        ]
        for role in (CUSTOMER, ENGINEER):
            outsider = get_user_model().objects.create_user("unrelated-" + role.replace(" ", "-"))
            outsider.groups.add(Group.objects.get(name=role))
            self.client.force_login(outsider)
            for path in paths:
                with self.subTest(role=role, path=path):
                    self.assertEqual(self.client.get(path).status_code, 404)

    def test_all_five_roles_are_installed(self):
        self.assertEqual(
            set(
                Group.objects.filter(
                    name__in=(CUSTOMER, ENGINEER, QUALITY, HOD, ADMIN)
                ).values_list("name", flat=True)
            ),
            set((CUSTOMER, ENGINEER, QUALITY, HOD, ADMIN)),
        )

    def test_customer_cannot_enter_internal_lab_even_if_job_owner(self):
        customer = get_user_model().objects.create_user("customer-boundary")
        customer.groups.add(Group.objects.get(name=CUSTOMER))
        job = Job.objects.create(owner=customer, title="Private job", customer="Fixture")
        self.client.force_login(customer)
        self.assertEqual(role_for(customer), CUSTOMER)
        self.assertFalse(lab_access(customer))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)
        self.assertEqual(self.client.get(reverse("customer_home")).status_code, 200)
        self.assertEqual(
            self.client.post(reverse("new_job"), {"title": "No", "customer": "No"}).status_code, 403
        )
        self.assertEqual(self.client.get(reverse("job_detail", args=[job.pk])).status_code, 404)

    def test_quality_sees_lab_jobs_but_cannot_create_one(self):
        engineer = get_user_model().objects.create_user("engineer-boundary")
        engineer.groups.add(Group.objects.get(name=ENGINEER))
        job = Job.objects.create(owner=engineer, title="Station job", customer="Fixture")
        quality = get_user_model().objects.create_user("quality-boundary")
        quality.groups.add(Group.objects.get(name=QUALITY))
        self.client.force_login(quality)
        self.assertEqual(self.client.get(reverse("job_detail", args=[job.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("new_job")).status_code, 403)

    def test_only_issuance_roles_can_prepare_customer_delivery(self):
        engineer = get_user_model().objects.create_user("delivery-engineer")
        engineer.groups.add(Group.objects.get(name=ENGINEER))
        job = Job.objects.create(owner=engineer, title="Issued job", customer="Fixture")
        TestRun.objects.create(job=job, test_type="work_instruction", assigned_to=engineer)
        report = Report.objects.create(
            job=job,
            revision=1,
            snapshot={},
            snapshot_sha256="0" * 64,
            created_by=engineer,
            approved_by=engineer,
            approved_at=timezone.now(),
        )
        report.approved_pdf.save("role-boundary.pdf", ContentFile(b"%PDF-1.4\n"), save=True)
        url = reverse("report_delivery", args=[report.pk])
        self.client.force_login(engineer)
        self.assertEqual(self.client.get(url).status_code, 403)
        quality = get_user_model().objects.create_user("delivery-quality")
        quality.groups.add(Group.objects.get(name=QUALITY))
        self.client.force_login(quality)
        self.assertEqual(self.client.get(url).status_code, 403)
        hod = get_user_model().objects.create_user("delivery-hod")
        hod.groups.add(Group.objects.get(name=HOD))
        self.client.force_login(hod)
        self.assertEqual(self.client.get(url).status_code, 200)

    @override_settings(DEBUG=False, LOCAL_LEGACY_ACCESS=False)
    def test_unassigned_production_user_is_denied(self):
        user = get_user_model().objects.create_user("unassigned-boundary")
        self.client.force_login(user)
        self.assertFalse(lab_access(user))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 403)
