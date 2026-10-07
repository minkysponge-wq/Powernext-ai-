from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .extraction import get_schema
from .models import AuditEvent, Job, TestRun
from .roles import ADMIN, CUSTOMER, ENGINEER, QUALITY


class StationWorkflowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user("station-admin")
        self.admin.groups.add(Group.objects.get(name=ADMIN))
        self.engineer = User.objects.create_user("station-engineer")
        self.engineer.groups.add(Group.objects.get(name=ENGINEER))
        self.other = User.objects.create_user("other-engineer")
        self.other.groups.add(Group.objects.get(name=ENGINEER))
        self.quality = User.objects.create_user("station-quality")
        self.quality.groups.add(Group.objects.get(name=QUALITY))
        self.job = Job.objects.create(owner=self.admin, title="Station fixture", customer="Fixture")

    def test_assignment_lock_reopen_and_reading_immutability(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("assign_test", args=[self.job.pk]),
            {
                "test_type": "pressure_oil_leakage",
                "station": "Bay 2",
                "assigned_to": self.engineer.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        run = TestRun.objects.get(job=self.job)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse("start_test", args=[run.pk])).status_code, 404)
        self.client.force_login(self.engineer)
        self.assertEqual(self.client.post(reverse("start_test", args=[run.pk])).status_code, 302)
        self.assertEqual(
            self.client.post(reverse("lock_test", args=[run.pk]), {"confirmed": "yes"}).status_code,
            400,
        )
        fields = list(self.job.fields.order_by("pk"))
        self.assertEqual(len(fields), len(get_schema(run.test_type)["fields"]))
        self.job.fields.update(status="not_applicable")
        for key, value in [
            ("series", "T1"),
            ("customer", "Fixture"),
            ("oil_observation", "No leakage observed"),
        ]:
            field = self.job.fields.get(context__schema_key=key)
            field.value = value
            field.status = "verified"
            field.save()
        self.assertEqual(
            self.client.post(reverse("lock_test", args=[run.pk]), {"confirmed": "yes"}).status_code,
            302,
        )
        run.refresh_from_db()
        self.assertEqual(run.status, "locked")
        fields[0].refresh_from_db()
        fields[0].value = "changed"
        with self.assertRaises(ValidationError):
            fields[0].save()
        self.assertEqual(
            self.client.post(reverse("lock_test", args=[run.pk]), {"confirmed": "yes"}).status_code,
            409,
        )
        self.client.force_login(self.quality)
        self.assertEqual(
            self.client.post(reverse("reopen_test", args=[run.pk]), {"reason": ""}).status_code, 400
        )
        self.assertEqual(
            self.client.post(
                reverse("reopen_test", args=[run.pk]), {"reason": "Check pressure reading"}
            ).status_code,
            302,
        )
        run.refresh_from_db()
        self.assertEqual(run.status, "in_progress")
        fields[0].save()
        self.assertTrue(AuditEvent.objects.filter(job=self.job, action="test_reopened").exists())

    def test_station_screen_is_scoped_and_save_keeps_engineer_in_test(self):
        own = TestRun.objects.create(
            job=self.job,
            test_type="pressure_oil_leakage",
            station="Bay 2",
            assigned_to=self.engineer,
        )
        other = TestRun.objects.create(
            job=self.job, test_type="work_instruction", station="Bay 3", assigned_to=self.other
        )
        self.client.force_login(self.engineer)
        self.assertEqual(self.client.post(reverse("start_test", args=[own.pk])).status_code, 302)
        url = reverse("digital_review", args=[self.job.pk]) + "?test=pressure_oil_leakage"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bay 2")
        self.assertEqual(response.context["selected_run"].pk, own.pk)
        self.assertEqual(
            self.client.get(
                reverse("digital_review", args=[self.job.pk]) + "?test=work_instruction"
            ).status_code,
            403,
        )
        rows = list(response.context["page"].object_list)
        data = {
            "form-TOTAL_FORMS": len(rows),
            "form-INITIAL_FORMS": len(rows),
            "form-MIN_NUM_FORMS": 0,
            "form-MAX_NUM_FORMS": 1000,
            "action": "save",
        }
        for i, field in enumerate(rows):
            data.update(
                {
                    f"form-{i}-id": field.pk,
                    f"form-{i}-value": "25" if i == 0 else "",
                    f"form-{i}-unit": field.unit,
                    f"form-{i}-status": "verified" if i == 0 else "not_applicable",
                    f"form-{i}-version": field.version,
                }
            )
        saved = self.client.post(url, data)
        self.assertEqual(saved.status_code, 302)
        self.assertIn("test=pressure_oil_leakage", saved["Location"])
        self.assertEqual(self.job.fields.filter(context__form_type=other.test_type).count(), 0)
        response = self.client.get(url)
        rows = list(response.context["page"].object_list)
        data["form-0-value"] = "26"
        for i, field in enumerate(rows):
            data[f"form-{i}-version"] = field.version
        autosaved = self.client.post(url, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(autosaved.status_code, 200)
        self.assertTrue(autosaved.json()["saved"])
        self.assertEqual(self.job.fields.get(pk=rows[0].pk).value, "26")
        self.assertEqual(autosaved.json()["versions"][str(rows[0].pk)], rows[0].version + 1)

    def test_lock_requires_identity_result_and_request_match(self):
        run = TestRun.objects.create(
            job=self.job,
            test_type="pressure_oil_leakage",
            station="Bay 2",
            assigned_to=self.engineer,
        )
        self.client.force_login(self.engineer)
        self.client.post(reverse("start_test", args=[run.pk]))
        self.job.fields.update(status="not_applicable")
        url = reverse("lock_test", args=[run.pk])
        self.assertContains(
            self.client.post(url, {"confirmed": "yes"}),
            "Required test identifiers",
            status_code=400,
        )
        series = self.job.fields.get(context__schema_key="series")
        series.value = "T1"
        series.status = "verified"
        series.save()
        customer = self.job.fields.get(context__schema_key="customer")
        customer.value = "Wrong customer"
        customer.status = "verified"
        customer.save()
        self.assertContains(
            self.client.post(url, {"confirmed": "yes"}), "configured result", status_code=400
        )
        observation = self.job.fields.get(context__schema_key="oil_observation")
        observation.value = "No leakage observed"
        observation.status = "verified"
        observation.save()
        self.assertContains(
            self.client.post(url, {"confirmed": "yes"}),
            "disagree with the registered request",
            status_code=400,
        )
        customer.value = self.job.customer
        customer.save()
        self.assertEqual(self.client.post(url, {"confirmed": "yes"}).status_code, 302)

    def test_bulk_na_requires_reviewed_result_and_preserves_entered_values(self):
        run = TestRun.objects.create(
            job=self.job,
            test_type="pressure_oil_leakage",
            station="Bay 2",
            assigned_to=self.engineer,
        )
        self.client.force_login(self.engineer)
        self.client.post(reverse("start_test", args=[run.pk]))
        url = reverse("mark_unused_not_applicable", args=[run.pk])
        payload = {
            "confirmed": "yes",
            "reason": "Only the oil-leakage branch applies to this station run.",
        }
        self.assertEqual(self.client.post(url, payload).status_code, 400)
        for key, value in [
            ("series", "T1"),
            ("customer", "Fixture"),
            ("oil_observation", "No leakage observed"),
        ]:
            field = self.job.fields.get(context__schema_key=key)
            field.value = value
            field.status = "verified"
            field.save()
        entered = self.job.fields.get(context__schema_key="oil_pressure")
        entered.value = "25"
        entered.save()
        self.assertEqual(
            self.client.post(url, {"confirmed": "yes", "reason": "short"}).status_code, 400
        )
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 302)
        self.assertIn("test=pressure_oil_leakage", response["Location"])
        entered.refresh_from_db()
        self.assertEqual(entered.status, "unreviewed")
        required = self.job.fields.get(context__schema_key="series")
        self.assertEqual(required.status, "verified")
        changed = self.job.fields.filter(status="not_applicable").count()
        self.assertGreater(changed, 40)
        self.assertEqual(
            AuditEvent.objects.filter(job=self.job, action="reading_marked_not_applicable").count(),
            changed,
        )
        self.assertEqual(
            self.client.post(reverse("lock_test", args=[run.pk]), {"confirmed": "yes"}).status_code,
            400,
        )
        entered.status = "verified"
        entered.save()
        self.assertEqual(
            self.client.post(reverse("lock_test", args=[run.pk]), {"confirmed": "yes"}).status_code,
            302,
        )
        self.client.force_login(self.quality)
        report = self.job.reports.first()
        review = self.client.get(reverse("quality_review", args=[report.pk]))
        self.assertContains(review, "Not-applicable readings")
        self.assertContains(review, payload["reason"])


class AutomaticReportFlowTests(TestCase):
    def test_customer_request_to_station_lock_creates_one_draft_and_notices(self):
        User = get_user_model()
        customer = User.objects.create_user("auto-customer")
        customer.groups.add(Group.objects.get(name=CUSTOMER))
        admin = User.objects.create_user("auto-admin")
        admin.groups.add(Group.objects.get(name=ADMIN))
        engineer = User.objects.create_user("auto-engineer")
        engineer.groups.add(Group.objects.get(name=ENGINEER))
        self.client.force_login(customer)
        request_data = {
            "customer": "Fictional Transformer Co., Ltd.",
            "customer_address": "12 Test Road, Bengaluru",
            "manufacturer": "Maker & Co.",
            "sample_particulars": "250 kVA transformer",
            "requested_tests": ["pressure_oil_leakage"],
        }
        self.assertEqual(
            self.client.post(reverse("customer_new_request"), request_data).status_code, 302
        )
        job = Job.objects.get(customer_user=customer)
        self.assertEqual(job.request_snapshot, request_data)
        self.assertEqual(job.test_runs.count(), 1)

        self.client.force_login(admin)
        scope = {
            "customer": request_data["customer"],
            "sample_code": "SAMPLE-001",
            "test_series": "SERIES-001",
            "report_scope": ["pressure_oil_leakage"],
            "scope_note": "Customer requested the pressure and oil-leakage test only.",
            "version": job.version,
        }
        self.assertEqual(
            self.client.post(reverse("job_scope", args=[job.pk]), scope).status_code, 302
        )
        self.assertEqual(
            self.client.post(
                reverse("assign_test", args=[job.pk]),
                {
                    "test_type": "pressure_oil_leakage",
                    "station": "Oil-leakage bay",
                    "assigned_to": engineer.pk,
                },
            ).status_code,
            302,
        )
        run = job.test_runs.get()

        self.client.force_login(engineer)
        self.assertEqual(self.client.post(reverse("start_test", args=[run.pk])).status_code, 302)
        customer_field = job.fields.get(context__schema_key="customer")
        self.assertEqual(customer_field.value, request_data["customer"])
        for key, value in [
            ("series", "SERIES-001"),
            ("customer", request_data["customer"]),
            ("oil_observation", "No leakage observed"),
        ]:
            field = job.fields.get(context__schema_key=key)
            field.value = value
            field.status = "verified"
            field.save()
        reason = "The remaining test branches were not part of this customer request."
        self.assertEqual(
            self.client.post(
                reverse("mark_unused_not_applicable", args=[run.pk]),
                {"confirmed": "yes", "reason": reason},
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(reverse("lock_test", args=[run.pk]), {"confirmed": "yes"}).status_code,
            302,
        )
        run.refresh_from_db()
        self.assertEqual(run.status, "locked")
        report = job.reports.get()
        self.assertEqual(report.snapshot["customer"], request_data["customer"])
        self.assertEqual(report.snapshot["customer_address"], request_data["customer_address"])
        self.assertEqual(report.snapshot["manufacturer"], request_data["manufacturer"])
        self.assertEqual(report.snapshot["sample_particulars"], request_data["sample_particulars"])
        self.assertEqual(job.events.filter(action="draft_snapshot_created").count(), 1)
        self.assertEqual(
            set(job.customer_notifications.values_list("kind", flat=True)),
            {f"test_locked_{run.pk}", "all_tests_complete"},
        )
        self.assertEqual(
            self.client.post(reverse("lock_test", args=[run.pk]), {"confirmed": "yes"}).status_code,
            409,
        )
        self.assertEqual(job.reports.count(), 1)
        self.assertEqual(job.customer_notifications.count(), 2)
