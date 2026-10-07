import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


def private_path(instance, filename):
    return f"sources/{instance.job_id}/{uuid.uuid4().hex}.pdf"


def intake_path(instance, filename):
    return f"intake/{uuid.uuid4().hex}.pdf"


class Job(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    customer_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="customer_jobs",
    )
    file_number = models.CharField(max_length=30, null=True, blank=True, unique=True)
    title = models.CharField(max_length=160)
    customer = models.CharField(max_length=200)
    customer_address = models.TextField(blank=True)
    manufacturer = models.CharField(max_length=200, blank=True)
    sample_particulars = models.TextField(blank=True)
    requested_tests = models.JSONField(default=list, blank=True)
    request_snapshot = models.JSONField(default=dict, blank=True)
    request_submitted_at = models.DateTimeField(null=True, blank=True)
    sample_code = models.CharField(max_length=100, blank=True)
    test_series = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    version = models.PositiveIntegerField(default=0)
    report_scope = models.JSONField(default=list, blank=True)
    report_test_ids = models.JSONField(default=list, blank=True)
    scope_note = models.TextField(blank=True)
    rules = models.ManyToManyField("Rule", blank=True, related_name="jobs")
    report_template = models.ForeignKey(
        "ReportTemplate", null=True, blank=True, on_delete=models.PROTECT
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class CustomerRequestDraft(models.Model):
    """One private, unfinished request per customer; never used as report evidence."""

    customer_user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    data = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)


class TestRun(models.Model):
    """One configured test station assignment within a customer's job."""

    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="test_runs")
    test_type = models.CharField(max_length=80)
    station = models.CharField(max_length=100, blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assigned_tests",
    )
    status = models.CharField(
        max_length=20,
        default="pending",
        choices=[
            ("pending", "Pending"),
            ("in_progress", "In progress"),
            ("locked", "Engineer locked"),
        ],
    )
    version = models.PositiveIntegerField(default=0)
    locked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="locked_tests",
    )
    locked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["job", "test_type"], name="unique_job_test_type")
        ]

    def __str__(self):
        return f"{self.job_id} / {self.test_type} / {self.status}"


class Document(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="documents")
    original_name = models.CharField(max_length=255)
    file = models.FileField(upload_to=private_path)
    sha256 = models.CharField(max_length=64)
    page_count = models.PositiveIntegerField()
    form_type = models.CharField(max_length=80, blank=True)
    status = models.CharField(
        max_length=30,
        default="queued",
        choices=[
            ("queued", "Queued"),
            ("ready", "Ready for review"),
            ("failed", "Processing failed"),
        ],
    )
    extracted_text = models.TextField(blank=True)
    extraction_data = models.JSONField(default=dict, blank=True)
    extraction_checkpoint = models.JSONField(default=dict, blank=True)
    processing_note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["job", "sha256"], name="unique_job_document_hash")
        ]


class IntakeItem(models.Model):
    """Private staging area for batch PDFs awaiting safe routing."""

    file = models.FileField(upload_to=intake_path)
    original_name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    page_count = models.PositiveIntegerField()
    candidate_job = models.ForeignKey(Job, null=True, blank=True, on_delete=models.PROTECT)
    candidate_form_type = models.CharField(max_length=80, blank=True)
    status = models.CharField(
        max_length=20,
        default="queued",
        choices=[
            ("queued", "Classifying"),
            ("triage", "Needs routing"),
            ("routed", "Routed"),
            ("failed", "Invalid PDF"),
        ],
    )
    reason = models.CharField(max_length=300)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    routed_document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class ReportTemplate(models.Model):
    name = models.CharField(max_length=100)
    version = models.PositiveIntegerField(default=1)
    definition = models.JSONField()
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["name", "version"], name="unique_report_template_version"
            )
        ]

    def clean(self):
        from .template_mapping import validate_definition

        validate_definition(self.definition)
        if self.pk and Report.objects.filter(snapshot__template_mapping__id=self.pk).exists():
            previous = type(self).objects.get(pk=self.pk)
            if (previous.name, previous.version, previous.definition) != (
                self.name,
                self.version,
                self.definition,
            ):
                from django.core.exceptions import ValidationError

                raise ValidationError(
                    "This template is used by a saved report. Create a new version instead."
                )

    def __str__(self):
        return f"{self.name} v{self.version}"


class Field(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="fields")
    key = models.CharField(max_length=200)
    label = models.CharField(max_length=200)
    value = models.TextField(blank=True)
    unit = models.CharField(max_length=30, blank=True)
    raw_value = models.TextField(blank=True)
    origin = models.CharField(
        max_length=20, choices=[("scan", "Scanned document"), ("digital", "Digital entry")]
    )
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.PROTECT)
    page = models.PositiveIntegerField(null=True, blank=True)
    bbox = models.JSONField(
        null=True, blank=True, help_text="Normalised [left, top, right, bottom] on original page"
    )
    status = models.CharField(
        max_length=20,
        default="unreviewed",
        choices=[
            ("unreviewed", "Needs review"),
            ("verified", "Verified"),
            ("ambiguous", "Ambiguous"),
            ("not_applicable", "Not applicable"),
        ],
    )
    context = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=0)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "key"], name="unique_job_field")]
        ordering = ["key"]

    def save(self, *args, **kwargs):
        form_type = self.context.get("form_type") if isinstance(self.context, dict) else None
        if (
            self.job_id
            and form_type
            and TestRun.objects.filter(
                job_id=self.job_id, test_type=form_type, status="locked"
            ).exists()
        ):
            from django.core.exceptions import ValidationError

            raise ValidationError(
                "This station test is locked. Quality must return it for correction before readings can change."
            )
        return super().save(*args, **kwargs)


class Rule(models.Model):
    code = models.CharField(max_length=100)
    version = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=200)
    operation = models.CharField(
        max_length=40,
        choices=[
            ("identity", "Direct value"),
            ("maximum", "Maximum"),
            ("minimum", "Minimum"),
            ("difference", "Difference"),
            ("ratio", "Ratio"),
            ("percent_deviation", "Percent deviation from nominal"),
            ("absolute_percent_change", "Absolute percent change"),
            ("all_text", "All observations match"),
        ],
    )
    parameters = models.JSONField(default=dict)
    source_clause = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        default="assumed",
        choices=[("assumed", "Provisional"), ("confirmed", "Confirmed")],
    )

    def clean(self):
        from decimal import InvalidOperation

        from django.core.exceptions import ValidationError

        from .calculations import OPERATIONS, finite

        errors = {}
        if self.operation not in OPERATIONS:
            errors["operation"] = "Choose a supported safe operation."
        if self.status == "confirmed" and not self.source_clause.strip():
            errors["source_clause"] = "A confirmed rule needs its exact source clause."
        if (
            self.version >= 2
            and self.status == "confirmed"
            and (
                "clause TBC" in self.source_clause
                or (
                    isinstance(self.parameters, dict)
                    and self.parameters.get("decision_status")
                    == "reviewed by team, pending CPRI adoption"
                )
            )
        ):
            errors["source_clause"] = (
                "CPRI adoption and an exact clause are required before confirmation."
            )
        params = self.parameters
        if not isinstance(params, dict) or not isinstance(params.get("unit"), str):
            errors["parameters"] = "Provide a parameters object with an explicit unit."
        else:
            inputs = params.get("inputs", params.get("fields"))
            if not isinstance(inputs, list) or not inputs or len(inputs) > 20:
                errors["parameters"] = "Provide 1–20 input selectors or field keys."
            for key in ("lower", "upper", "tolerance", "marginal_percent"):
                if key in params:
                    try:
                        number = finite(params[key], key)
                        if key == "tolerance" and number < 0:
                            errors["parameters"] = "Tolerance cannot be negative."
                        if key == "marginal_percent" and not 0 <= number <= 100:
                            errors["parameters"] = (
                                "Marginal band must be between 0 and 100 percent."
                            )
                    except (ValueError, TypeError, InvalidOperation):
                        errors["parameters"] = f"{key} must be a finite number."
            if self.operation == "all_text" and not params.get("accepted_values"):
                errors["parameters"] = "Text rules need explicit accepted_values."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.code} v{self.version}: {self.title} ({self.status})"

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["code", "version"], name="unique_rule_version")
        ]


class Report(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="reports")
    revision = models.PositiveIntegerField()
    template_version = models.CharField(max_length=40, default="proposed-v1")
    snapshot = models.JSONField()
    search_text = models.TextField(blank=True)
    snapshot_sha256 = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    engineer_locked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="locked_lab_reports",
    )
    engineer_locked_at = models.DateTimeField(null=True, blank=True)
    quality_verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="quality_verified_lab_reports",
    )
    quality_verified_at = models.DateTimeField(null=True, blank=True)
    returned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="returned_lab_reports",
    )
    returned_at = models.DateTimeField(null=True, blank=True)
    return_reason = models.CharField(max_length=500, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approved_lab_reports",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    issue_seal = models.CharField(max_length=255, blank=True)
    approved_pdf = models.FileField(upload_to="approved-reports/", blank=True)
    approved_pdf_sha256 = models.CharField(max_length=64, blank=True)
    delivery_status = models.CharField(
        max_length=20,
        default="pending",
        choices=[
            ("pending", "Not sent"),
            ("sending", "Sending / reconcile before retry"),
            ("sent", "Sent"),
            ("acknowledged", "Customer acknowledged"),
        ],
    )
    delivery_recipient = models.EmailField(blank=True)
    delivery_note = models.CharField(max_length=500, blank=True)
    delivery_version = models.PositiveIntegerField(default=0)

    class Meta:
        permissions = [
            ("lock_report", "Lock engineer data for quality review"),
            ("verify_report", "Verify a locked laboratory report as quality reviewer"),
            ("approve_report", "Approve a laboratory report for export as HoD"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["job", "revision"], name="unique_report_revision")
        ]
        ordering = ["-revision"]


class InsightDraft(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="insight_drafts")
    evidence_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=20, default="queued")
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processing_token = models.CharField(max_length=32, blank=True)
    content = models.JSONField(default=list)
    provider = models.CharField(max_length=100, blank=True)
    message = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class AppendOnlyAuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValueError("Audit events are append-only.")

    def delete(self):
        raise ValueError("Audit events are append-only.")

    def bulk_create(self, *args, **kwargs):
        raise ValueError("Create audit events one at a time to preserve the hash chain.")

    def bulk_update(self, *args, **kwargs):
        raise ValueError("Audit events are append-only.")


class AuditHead(models.Model):
    job = models.OneToOneField(Job, on_delete=models.PROTECT, related_name="audit_head")
    last_hash = models.CharField(max_length=64, default="0" * 64)
    event_count = models.PositiveIntegerField(default=0)


class AuditEvent(models.Model):
    objects = AppendOnlyAuditQuerySet.as_manager()
    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=100)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)
    previous_hash = models.CharField(max_length=64, blank=True)
    event_hash = models.CharField(max_length=64, blank=True)

    def save(self, *args, **kwargs):
        from django.db import transaction

        from .audit_chain import event_digest

        if self.pk:
            raise ValueError("Audit events are append-only.")
        with transaction.atomic():
            Job.objects.select_for_update().get(pk=self.job_id)
            head, _ = AuditHead.objects.select_for_update().get_or_create(job_id=self.job_id)
            self.previous_hash = head.last_hash
            self.event_hash = event_digest(self)
            result = super().save(*args, **kwargs)
            head.last_hash = self.event_hash
            head.event_count += 1
            head.save(update_fields=["last_hash", "event_count"])
            return result

    def delete(self, *args, **kwargs):
        raise ValueError("Audit events are append-only.")

    class Meta:
        ordering = ["-created_at"]


class CustomerNotification(models.Model):
    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="customer_notifications")
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="lab_notifications"
    )
    kind = models.CharField(max_length=40)
    message = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["job", "kind"], name="unique_customer_job_notification")
        ]
        ordering = ["-created_at"]
