from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from lab.models import Field, Job
from lab.quality import assess, sensitive_separator_change


class SensitiveSeparatorChangeTests(SimpleTestCase):
    def field(self, key, raw, current, status="unreviewed", origin="scan"):
        return SimpleNamespace(
            document=None,
            context={"schema_key": key},
            page=1,
            key=key,
            raw_value=raw,
            value=current,
            status=status,
            origin=origin,
        )

    def test_date_and_identifier_separator_changes_are_flagged(self):
        self.assertTrue(sensitive_separator_change(self.field("date_BT", "29.10.25", "29-10-25")))
        self.assertTrue(
            sensitive_separator_change(self.field("sample_code", "HVD-25/0847", "HVD-25-0847"))
        )

    def test_unrelated_and_engineer_confirmed_values_are_not_flagged(self):
        self.assertFalse(
            sensitive_separator_change(
                self.field("hv_observation_BT", "No discharge.", "No discharge")
            )
        )
        self.assertFalse(
            sensitive_separator_change(
                self.field("date_BT", "29.10.25", "29-10-25", status="verified")
            )
        )
        self.assertFalse(
            sensitive_separator_change(
                self.field("date_BT", "29.10.25", "29-10-25", origin="digital")
            )
        )


class SensitiveSeparatorIntegrationTests(TestCase):
    def test_changed_date_separator_is_a_failed_review_check(self):
        user = get_user_model().objects.create_user("separator-reviewer")
        job = Job.objects.create(owner=user, title="Separator check", customer="Fixture")
        field = Field.objects.create(
            job=job,
            key="source.date_BT",
            label="Before-test date",
            raw_value="29.10.25",
            value="29-10-25",
            origin="scan",
            status="unreviewed",
            updated_by=user,
            context={"form_type": "routine_test", "schema_key": "date_BT"},
        )
        check = assess([field])[field.pk]
        self.assertEqual(check["state"], "needs_review")
        self.assertTrue(any("separators changed" in issue for issue in check["failed"]))
