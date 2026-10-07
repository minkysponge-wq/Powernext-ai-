"""Assigned station work. Test locks are audited and cannot be undone by the engineer."""

from urllib.parse import urlencode

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .extraction import get_schema
from .models import AuditEvent, Field, Job, TestRun
from .notifications import notify_customer
from .quality import identity
from .report_workflow import job_locked
from .roles import ADMIN, ENGINEER, HOD, QUALITY, role_for
from .test_catalog import station_type, station_types
from .views import allowed_jobs


class AssignmentForm(forms.Form):
    test_type = forms.ChoiceField(label="Configured test type")
    station = forms.CharField(max_length=100)
    assigned_to = forms.ModelChoiceField(
        queryset=get_user_model().objects.none(), label="Test engineer"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["test_type"].choices = [(s["form_type"], s["label"]) for s in station_types()]
        self.fields["assigned_to"].queryset = (
            get_user_model().objects.filter(groups__name=ENGINEER, is_active=True).distinct()
        )


@login_required
def assign_test(request, pk):
    if role_for(request.user) != ADMIN:
        return HttpResponse("Lab administrator access required.", status=403)
    job = get_object_or_404(allowed_jobs(request.user), pk=pk)
    if request.method == "POST" and job_locked(job):
        return HttpResponse("Report data is locked for review or issuance.", status=409)
    form = AssignmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            job = Job.objects.select_for_update().get(pk=pk)
            if job_locked(job):
                return HttpResponse("Report data is locked for review or issuance.", status=409)
            test_type = form.cleaned_data["test_type"]
            run = job.test_runs.select_for_update().filter(test_type=test_type).first()
            if run and run.assigned_to_id:
                form.add_error(
                    "test_type",
                    "This test type already has an engineer. Return the assignment before changing it.",
                )
            else:
                if run:
                    run.station = form.cleaned_data["station"]
                    run.assigned_to = form.cleaned_data["assigned_to"]
                    run.version += 1
                    run.save(update_fields=["station", "assigned_to", "version"])
                else:
                    run = TestRun.objects.create(
                        job=job,
                        test_type=test_type,
                        station=form.cleaned_data["station"],
                        assigned_to=form.cleaned_data["assigned_to"],
                    )
                AuditEvent.objects.create(
                    job=job,
                    actor=request.user,
                    action="test_assigned",
                    details={
                        "test_run": run.pk,
                        "test_type": test_type,
                        "station": run.station,
                        "engineer": run.assigned_to_id,
                    },
                )
                return redirect("job_detail", pk=pk)
    return render(
        request,
        "lab/form.html",
        {"form": form, "job": job, "title": "Assign a test station", "button": "Assign test"},
    )


@login_required
@require_POST
def start_test(request, pk):
    with transaction.atomic():
        run = get_object_or_404(TestRun.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
        job = Job.objects.select_for_update().get(pk=run.job_id)
        run = TestRun.objects.select_for_update().get(pk=pk)
        if job_locked(job) or run.status != "pending":
            return HttpResponse("Test cannot be started at this stage.", status=409)
        if run.assigned_to_id != request.user.pk or role_for(request.user) != ENGINEER:
            return HttpResponse(
                "Only the assigned test engineer can start this station test.", status=403
            )
        schema = get_schema(run.test_type)
        present = {
            (identity(f)[1], identity(f)[2])
            for f in job.fields.filter(document__isnull=True)
            if identity(f)[0] == run.test_type
        }
        missing = [item for item in schema["fields"] if (item["page"], item["key"]) not in present]
        inherited = {
            "customer": job.request_snapshot.get("customer") or job.customer,
            "manufacturer": job.request_snapshot.get("manufacturer") or job.manufacturer,
            "sample_code": job.sample_code,
            "series": job.test_series,
        }
        Field.objects.bulk_create(
            [
                Field(
                    job=job,
                    key=f"station.{run.pk}.p{item['page']}.{item['key']}",
                    label=item["label"],
                    value=inherited.get(item["key"], "") or "",
                    raw_value=inherited.get(item["key"], "") or "",
                    origin="digital",
                    status="unreviewed",
                    updated_by=request.user,
                    context={
                        "form_type": run.test_type,
                        "schema_page": item["page"],
                        "schema_key": item["key"],
                        "source_reference": (
                            "Customer request"
                            if item["key"] in ("customer", "manufacturer") and job.request_snapshot
                            else (run.station or "Station entry")
                        ),
                        "test_run_id": run.pk,
                    },
                )
                for item in missing
            ]
        )
        run.status = "in_progress"
        run.version += 1
        run.save(update_fields=["status", "version"])
        AuditEvent.objects.create(
            job=job,
            actor=request.user,
            action="test_started",
            details={
                "test_run": run.pk,
                "test_type": run.test_type,
                "entry_fields_created": len(missing),
                "prefilled_from_request": sum(
                    bool(inherited.get(item["key"]))
                    for item in missing
                    if item["key"] in ("customer", "manufacturer")
                ),
            },
        )
    if request.POST.get("next") == "entry":
        return redirect(
            reverse("digital_review", kwargs={"pk": run.job_id})
            + "?"
            + urlencode({"test": run.test_type})
        )
    return redirect("job_detail", pk=run.job_id)


@login_required
@require_POST
def lock_test(request, pk):
    with transaction.atomic():
        run = get_object_or_404(TestRun.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
        if run.assigned_to_id != request.user.pk or role_for(request.user) != ENGINEER:
            return HttpResponse(
                "Only the assigned test engineer can lock this station test.", status=403
            )
        if request.POST.get("confirmed") != "yes":
            return HttpResponse("Confirm readings against station records.", status=400)
        job = Job.objects.select_for_update().get(pk=run.job_id)
        run = TestRun.objects.select_for_update().get(pk=pk)
        if job_locked(job) or run.status != "in_progress":
            return HttpResponse("Test is not open for locking.", status=409)
        if run.assigned_to_id != request.user.pk or role_for(request.user) != ENGINEER:
            return HttpResponse(
                "Only the assigned test engineer can lock this station test.", status=403
            )
        schema = get_schema(run.test_type)
        expected = {(f["page"], f["key"]) for f in schema["fields"]}
        fields = [
            f for f in job.fields.filter(document__isnull=True) if identity(f)[0] == run.test_type
        ]
        present = {(identity(f)[1], identity(f)[2]) for f in fields}
        if not fields or expected - present:
            return HttpResponse(
                f"{len(expected-present)} configured readings are missing. Complete the station form first.",
                status=400,
            )
        if any(f.status not in ("verified", "not_applicable") for f in fields):
            return HttpResponse(
                "Review every station reading or mark it not applicable before locking.", status=400
            )
        policy = station_type(run.test_type)
        verified = {
            identity(f)[2]: f.value.strip()
            for f in fields
            if f.status == "verified" and f.value.strip()
        }
        missing_required = [key for key in policy["lock_required"] if key not in verified]
        if missing_required:
            return HttpResponse(
                "Required test identifiers need reviewed values: " + ", ".join(missing_required),
                status=400,
            )
        if not any(key in verified for key in policy["result_any_of"]):
            return HttpResponse(
                "At least one configured result or observation needs a reviewed value before locking.",
                status=400,
            )
        original = job.request_snapshot or {}
        expected_identity = {
            "customer": original.get("customer") or job.customer,
            "manufacturer": original.get("manufacturer") or job.manufacturer,
            "sample_code": job.sample_code,
            "series": job.test_series,
        }
        conflicts = sorted(
            {
                key
                for f in fields
                if (key := identity(f)[2]) in expected_identity
                and expected_identity.get(key)
                and f.status == "verified"
                and f.value.strip() != str(expected_identity[key]).strip()
            }
        )
        if conflicts:
            return HttpResponse(
                "Station identifiers disagree with the registered request: " + ", ".join(conflicts),
                status=400,
            )
        run.status = "locked"
        run.locked_by = request.user
        run.locked_at = timezone.now()
        run.version += 1
        run.save(update_fields=["status", "locked_by", "locked_at", "version"])
        AuditEvent.objects.create(
            job=job,
            actor=request.user,
            action="test_locked",
            details={
                "test_run": run.pk,
                "test_type": run.test_type,
                "readings": len(fields),
                "locked_at": run.locked_at.isoformat(),
            },
        )
        notify_customer(
            job,
            f"test_locked_{run.pk}",
            f'{run.test_type.replace("_"," ").title()} is complete for {job.file_number or "your request"}.',
        )
        if not job.test_runs.exclude(status="locked").exists():
            notify_customer(
                job,
                "all_tests_complete",
                f'All planned tests are complete for {job.file_number or "your request"}. The report is entering review.',
            )
            from .report_builder import save_draft

            save_draft(job, request.user, trigger="all_station_tests_locked")
    return redirect("job_detail", pk=run.job_id)


@login_required
@require_POST
def mark_unused_not_applicable(request, pk):
    """Explicitly classify untouched optional cells; preserve every entered value and audit each change."""
    with transaction.atomic():
        run = get_object_or_404(TestRun.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
        if run.assigned_to_id != request.user.pk or role_for(request.user) != ENGINEER:
            return HttpResponse(
                "Only the assigned test engineer can classify these readings.", status=403
            )
        reason = request.POST.get("reason", "").strip()
        if request.POST.get("confirmed") != "yes" or not 10 <= len(reason) <= 500:
            return HttpResponse(
                "Confirm and give a 10–500 character reason for the non-applicable readings.",
                status=400,
            )
        job = Job.objects.select_for_update().get(pk=run.job_id)
        run = TestRun.objects.select_for_update().get(pk=pk)
        if job_locked(job) or run.status != "in_progress":
            return HttpResponse("This station test is not open for classification.", status=409)
        if run.assigned_to_id != request.user.pk or role_for(request.user) != ENGINEER:
            return HttpResponse(
                "Only the assigned test engineer can classify these readings.", status=403
            )
        policy = station_type(run.test_type)
        fields = [
            field
            for field in job.fields.select_for_update().filter(document__isnull=True)
            if identity(field)[0] == run.test_type
        ]
        verified = {
            identity(field)[2]
            for field in fields
            if field.status == "verified" and field.value.strip()
        }
        if not set(policy["lock_required"]).issubset(verified) or not verified.intersection(
            policy["result_any_of"]
        ):
            return HttpResponse(
                "Review required identifiers and at least one actual result first.", status=400
            )
        optional = [
            field
            for field in fields
            if field.status == "unreviewed"
            and not field.value.strip()
            and identity(field)[2] not in policy["lock_required"]
        ]
        if not optional:
            return HttpResponse("No untouched optional readings remain.", status=409)
        now = timezone.now()
        events = []
        for field in optional:
            field.status = "not_applicable"
            field.version += 1
            field.updated_by = request.user
            field.updated_at = now
            events.append(
                AuditEvent(
                    job=job,
                    actor=request.user,
                    action="reading_marked_not_applicable",
                    details={
                        "field_id": field.pk,
                        "key": field.key,
                        "before": {"value": "", "status": "unreviewed"},
                        "after": {"value": "", "status": "not_applicable"},
                        "reason": reason,
                        "test_run": run.pk,
                    },
                )
            )
        Field.objects.bulk_update(optional, ["status", "version", "updated_by", "updated_at"])
        for event in events:
            event.save()
        Job.objects.filter(pk=job.pk).update(version=F("version") + 1)
    return redirect(
        reverse("digital_review", kwargs={"pk": job.pk}) + "?" + urlencode({"test": run.test_type})
    )


@login_required
@require_POST
def reopen_test(request, pk):
    if role_for(request.user) not in (QUALITY, HOD, ADMIN):
        return HttpResponse("Quality or HoD authority is required to reopen a test.", status=403)
    with transaction.atomic():
        run = get_object_or_404(TestRun.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
        reason = request.POST.get("reason", "").strip()
        if not reason or len(reason) > 500:
            return HttpResponse("Give a correction reason (up to 500 characters).", status=400)
        job = Job.objects.select_for_update().get(pk=run.job_id)
        run = TestRun.objects.select_for_update().get(pk=pk)
        if job_locked(job) or run.status != "locked":
            return HttpResponse("The test cannot be reopened at this report stage.", status=409)
        run.status = "in_progress"
        run.version += 1
        run.save(update_fields=["status", "version"])
        AuditEvent.objects.create(
            job=job,
            actor=request.user,
            action="test_reopened",
            details={"test_run": run.pk, "test_type": run.test_type, "reason": reason},
        )
    return redirect("job_detail", pk=run.job_id)
