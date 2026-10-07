from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from .assembly import assemble
from .models import AuditEvent, CustomerRequestDraft, Job, TestRun
from .roles import ADMIN, CUSTOMER, ENGINEER, QUALITY


class CustomerPortalTests(TestCase):
    def setUp(self):
        self.customer = get_user_model().objects.create_user("request-customer")
        self.customer.groups.add(Group.objects.get(name=CUSTOMER))
        self.other = get_user_model().objects.create_user("other-customer")
        self.other.groups.add(Group.objects.get(name=CUSTOMER))
        self.client.force_login(self.customer)

    def test_request_creates_tracking_number_plan_and_exact_report_source(self):
        data = {
            "customer": "M/s Example Industries, Ltd.",
            "customer_address": "12 Test Road, Chennai",
            "manufacturer": "ABC Power & Co.",
            "sample_particulars": "Transformer serial X-01",
            "requested_tests": ["routine_test", "temperature_rise"],
        }
        self.assertEqual(self.client.post(reverse("customer_new_request"), data).status_code, 302)
        job = Job.objects.get(customer_user=self.customer)
        self.assertTrue(job.file_number.startswith("OV-"))
        self.assertEqual(job.manufacturer, data["manufacturer"])
        self.assertEqual(job.requested_tests, data["requested_tests"])
        self.assertEqual(
            set(job.test_runs.values_list("test_type", flat=True)), set(data["requested_tests"])
        )
        self.assertTrue(
            AuditEvent.objects.filter(job=job, action="customer_request_submitted").exists()
        )
        self.assertContains(
            self.client.get(reverse("customer_home") + "?q=" + job.file_number), job.file_number
        )
        self.assertNotContains(
            self.client.get(reverse("customer_home") + "?q=unrelated-number"), job.file_number
        )
        snapshot = assemble(job)
        for key in (
            "customer",
            "customer_address",
            "manufacturer",
            "sample_particulars",
            "file_number",
        ):
            self.assertEqual(snapshot[key], getattr(job, key))
        job.manufacturer = "Incorrect transcription"
        job.save(update_fields=["manufacturer"])
        checked = assemble(job)
        self.assertEqual(checked["manufacturer"], data["manufacturer"])
        self.assertTrue(
            any(x["id"] == "request-mismatch-manufacturer" for x in checked["findings"])
        )
        self.assertContains(
            self.client.get(reverse("customer_status", args=[job.file_number])),
            "0 of 2 tests complete",
        )
        run = job.test_runs.first()
        run.status = "locked"
        run.save(update_fields=["status"])
        self.assertContains(
            self.client.get(reverse("customer_status", args=[job.file_number])),
            "1 of 2 tests complete",
        )
        self.assertEqual(
            self.client.get(reverse("customer_download", args=[job.file_number])).status_code, 404
        )
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.get(reverse("customer_status", args=[job.file_number])).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse("customer_download", args=[job.file_number])).status_code, 404
        )
        engineer = get_user_model().objects.create_user("customer-boundary-engineer")
        engineer.groups.add(Group.objects.get(name=ENGINEER))
        self.client.force_login(engineer)
        self.assertEqual(
            self.client.get(reverse("customer_status", args=[job.file_number])).status_code, 403
        )

    def test_incomplete_request_creates_no_job(self):
        self.assertEqual(
            self.client.post(reverse("customer_new_request"), {"customer": "Name"}).status_code, 200
        )
        self.assertFalse(Job.objects.exists())

    def test_private_draft_restores_then_clears_on_submission(self):
        saved = self.client.post(
            reverse("customer_save_draft"),
            {
                "customer": "Draft Co",
                "manufacturer": "Draft maker",
                "requested_tests": ["routine_test", "routine_test", "unknown"],
            },
        )
        self.assertEqual(saved.status_code, 200)
        draft = CustomerRequestDraft.objects.get(customer_user=self.customer)
        self.assertEqual(draft.data["requested_tests"], ["routine_test"])
        self.assertContains(self.client.get(reverse("customer_new_request")), "Draft Co")
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse("customer_new_request")), "Draft Co")
        self.client.force_login(self.customer)
        data = {
            "customer": "Final Co",
            "customer_address": "12 Test Road",
            "manufacturer": "Maker",
            "sample_particulars": "Serial X",
            "requested_tests": ["routine_test"],
        }
        self.assertEqual(self.client.post(reverse("customer_new_request"), data).status_code, 302)
        self.assertFalse(CustomerRequestDraft.objects.filter(customer_user=self.customer).exists())

    def test_station_lock_updates_customer_and_creates_completion_notice(self):
        request = {
            "customer": "Example Lab Customer",
            "customer_address": "12 Test Road",
            "manufacturer": "ABC",
            "sample_particulars": "Sample X",
            "requested_tests": ["pressure_oil_leakage"],
        }
        self.client.post(reverse("customer_new_request"), request)
        job = Job.objects.get(customer_user=self.customer)
        admin = get_user_model().objects.create_user("customer-flow-admin")
        admin.groups.add(Group.objects.get(name=ADMIN))
        engineer = get_user_model().objects.create_user("customer-flow-engineer")
        engineer.groups.add(Group.objects.get(name=ENGINEER))
        self.client.force_login(admin)
        self.assertEqual(
            self.client.post(
                reverse("assign_test", args=[job.pk]),
                {
                    "test_type": "pressure_oil_leakage",
                    "station": "Bay A",
                    "assigned_to": engineer.pk,
                },
            ).status_code,
            302,
        )
        run = TestRun.objects.get(job=job)
        self.client.force_login(engineer)
        self.assertEqual(self.client.post(reverse("start_test", args=[run.pk])).status_code, 302)
        self.assertGreater(job.fields.count(), 0)
        self.assertEqual(job.fields.get(context__schema_key="customer").value, request["customer"])
        job.fields.update(status="not_applicable")
        for key, value in [
            ("series", "T1"),
            ("customer", request["customer"]),
            ("oil_observation", "No leakage observed"),
        ]:
            field = job.fields.get(context__schema_key=key)
            field.value = value
            field.status = "verified"
            field.save()
        self.assertEqual(
            self.client.post(reverse("lock_test", args=[run.pk]), {"confirmed": "yes"}).status_code,
            302,
        )
        self.assertEqual(
            set(job.customer_notifications.values_list("kind", flat=True)),
            {f"test_locked_{run.pk}", "all_tests_complete"},
        )
        self.assertEqual(job.reports.count(), 1)
        self.assertEqual(job.reports.first().snapshot["manufacturer"], request["manufacturer"])
        self.client.force_login(self.customer)
        self.assertContains(
            self.client.get(reverse("customer_status", args=[job.file_number])),
            "1 of 1 tests complete",
        )
        self.assertContains(
            self.client.get(reverse("customer_status", args=[job.file_number])),
            "All planned tests are complete",
        )
        quality = get_user_model().objects.create_user("customer-flow-quality")
        quality.groups.add(Group.objects.get(name=QUALITY))
        self.client.force_login(quality)
        report = job.reports.first()
        self.assertContains(
            self.client.get(reverse("quality_review", args=[report.pk])), "Exact match"
        )
        self.assertContains(self.client.get(reverse("audit_history", args=[job.pk])), "test_locked")
