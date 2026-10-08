"""Pre-freeze checks for station save validation and issued-number search."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Job, Report, TestRun
from .roles import ADMIN, ENGINEER
from .verification_link import issued_report_number


class StationSaveValidationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        admin = User.objects.create_user("validation-admin")
        admin.groups.add(Group.objects.get(name=ADMIN))
        engineer = User.objects.create_user("validation-engineer")
        engineer.groups.add(Group.objects.get(name=ENGINEER))
        self.job = Job.objects.create(owner=admin, title="Validation", customer="Synthetic")
        run = TestRun.objects.create(
            job=self.job, test_type="routine_test", station="Demo bay", assigned_to=engineer
        )
        self.client.force_login(engineer)
        self.assertEqual(self.client.post(reverse("start_test", args=[run.pk])).status_code, 302)
        self.target = self.job.fields.get(context__form_type="routine_test", context__schema_key="rated_power")
        all_fields = list(self.job.fields.filter(context__form_type="routine_test").order_by("key"))
        self.page_number = all_fields.index(self.target) // 30 + 1
        self.url = reverse("digital_review", args=[self.job.pk]) + f"?test=routine_test&page={self.page_number}"

    def save(self, value, unit):
        page = self.client.get(self.url)
        rows = list(page.context["page"].object_list)
        data = {
            "form-TOTAL_FORMS": len(rows),
            "form-INITIAL_FORMS": len(rows),
            "form-MIN_NUM_FORMS": 0,
            "form-MAX_NUM_FORMS": 1000,
            "action": "save",
        }
        for index, field in enumerate(rows):
            data.update({
                f"form-{index}-id": field.pk,
                f"form-{index}-value": value if field.pk == self.target.pk else field.value,
                f"form-{index}-unit": unit if field.pk == self.target.pk else field.unit,
                f"form-{index}-status": "verified" if field.pk == self.target.pk else field.status,
                f"form-{index}-version": field.version,
            })
        return self.client.post(self.url, data)

    def test_rejects_text_and_redisplays_entered_value(self):
        response = self.save("not a number", "kVA")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a finite numeric reading.")
        self.assertContains(response, "not a number")
        self.target.refresh_from_db()
        self.assertEqual(self.target.value, "")

    def test_rejects_wrong_unit_and_redisplays_it(self):
        response = self.save("250", "kg")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Use the configured unit: VA or kVA.")
        self.assertContains(response, "kg")
        self.target.refresh_from_db()
        self.assertEqual(self.target.value, "")

    def test_accepts_valid_station_value_and_unit(self):
        response = self.save("250", "kVA")
        self.assertEqual(response.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual((self.target.value, self.target.unit, self.target.status), ("250", "kVA", "verified"))


class ReportNumberSearchTests(TestCase):
    def test_dashboard_finds_issued_synthetic_report_number(self):
        user = get_user_model().objects.create_user("search-admin")
        user.groups.add(Group.objects.get(name=ADMIN))
        job = Job.objects.create(
            owner=user, title="Issued synthetic job", customer="Synthetic customer",
            sample_code="SYN-SEARCH-001", file_number="OV-2026-SEARCH",
        )
        report = Report.objects.create(
            job=job, revision=1, snapshot={"synthetic_demo": True, "sample_code": job.sample_code},
            snapshot_sha256="a" * 64, created_by=user, approved_at=timezone.now(),
        )
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard"), {"q": issued_report_number(report)})
        self.assertContains(response, job.sample_code)
        self.assertContains(response, job.customer)
