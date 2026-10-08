import csv
import hashlib
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, Q
from django.forms import modelformset_factory
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_q.tasks import async_task

from .assembly import assemble
from .forms import ReadingImportForm, ReviewRowForm, ScopeForm
from .models import AuditEvent, Field, InsightDraft, Job, Report
from .quality import identity
from .report_workflow import job_locked
from .roles import ENGINEER, can_manage_job, role_for, station_edit_allowed
from .views import allowed_jobs


@login_required
def scope(request, pk):
    job = get_object_or_404(allowed_jobs(request.user), pk=pk)
    if request.method == "POST" and not can_manage_job(request.user, job):
        return HttpResponse("Lab registration authority required.", status=403)
    if request.method == "POST" and job_locked(job):
        return HttpResponse("Report data is locked for review or issuance.", status=409)
    form = ScopeForm(request.POST or None, instance=job)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            current = Job.objects.select_for_update().get(pk=pk)
            if job_locked(current):
                return HttpResponse("Report data is locked for review or issuance.", status=409)
            if current.version != form.cleaned_data["version"]:
                return HttpResponse("This job changed. Reload before saving its scope.", status=409)
            job = form.save(commit=False)
            job.version = current.version + 1
            job.save()
            AuditEvent.objects.create(
                job=job,
                actor=request.user,
                action="report_scope_confirmed",
                details={"scope": job.report_scope, "individual_tests": job.report_test_ids},
            )
        return redirect("findings", pk=pk)
    return render(
        request,
        "lab/form.html",
        {
            "form": form,
            "job": job,
            "title": "Confirm report scope and identity",
            "button": "Save report scope",
        },
    )


@login_required
def import_readings(request, pk):
    job = get_object_or_404(allowed_jobs(request.user), pk=pk)
    if request.method == "POST" and job_locked(job):
        return HttpResponse("Report data is locked for review or issuance.", status=409)
    form = ReadingImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        from .structured_import import import_csv

        try:
            count = import_csv(
                job, form.cleaned_data["file"], form.cleaned_data["source_reference"], request.user
            )
            messages.success(
                request,
                (
                    f"{count} new readings imported for review."
                    if count
                    else "This file is already imported; no duplicates created."
                ),
            )
            return redirect("digital_review", pk=pk)
        except (ValueError, csv.Error) as error:
            form.add_error("file", str(error))
    return render(request, "lab/import.html", {"form": form, "job": job})


@login_required
def import_template(request, pk):
    get_object_or_404(allowed_jobs(request.user), pk=pk)
    from .extraction import schemas

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="laboratory-readings-template.csv"'
    writer = csv.writer(response)
    writer.writerow(["form_type", "page", "key", "value", "unit"])
    for schema in schemas():
        for field in schema["fields"]:
            writer.writerow([schema["form_type"], field["page"], field["key"], "", ""])
    return response


@login_required
def digital_review(request, pk):
    job = get_object_or_404(allowed_jobs(request.user), pk=pk)
    if request.method == "POST" and job_locked(job):
        return HttpResponse("Report data is locked for review or issuance.", status=409)
    selected_type = request.GET.get("test", "").strip()
    digital = job.fields.filter(document__isnull=True)
    if role_for(request.user) == ENGINEER:
        assigned = list(
            job.test_runs.filter(assigned_to=request.user)
            .exclude(status="locked")
            .values_list("test_type", flat=True)
        )
        if selected_type and selected_type not in assigned:
            return HttpResponse("This test is not assigned to your station.", status=403)
        digital = digital.filter(context__form_type__in=assigned)
    elif not can_manage_job(request.user, job):
        return HttpResponse("Station access required.", status=403)
    if selected_type:
        if not job.test_runs.filter(test_type=selected_type).exclude(status="locked").exists():
            return HttpResponse("This station test is unavailable for editing.", status=409)
        digital = digital.filter(context__form_type=selected_type)
    selected_run = job.test_runs.filter(test_type=selected_type).first() if selected_type else None
    from .test_catalog import station_type

    lock_policy = station_type(selected_type) if selected_run else None
    total = digital.count()
    remaining = digital.exclude(status__in=["verified", "not_applicable"]).count()
    ready_for_bulk = False
    optional_blank_count = 0
    lock_blockers = []
    if (
        selected_run
        and selected_run.status == "in_progress"
        and selected_run.assigned_to_id == request.user.pk
    ):
        candidates = list(digital)
        verified_keys = {
            identity(field)[2]
            for field in candidates
            if field.status == "verified" and field.value.strip()
        }
        missing_required = [key for key in lock_policy["lock_required"] if key not in verified_keys]
        if remaining:
            lock_blockers.append(f"{remaining} readings still need a review status")
        if missing_required:
            lock_blockers.append("Required reviewed values: " + ", ".join(missing_required))
        if not verified_keys.intersection(lock_policy["result_any_of"]):
            lock_blockers.append("At least one measured result or observation must be reviewed")
        ready_for_bulk = set(lock_policy["lock_required"]).issubset(verified_keys) and bool(
            verified_keys.intersection(lock_policy["result_any_of"])
        )
        optional_blank_count = sum(
            field.status == "unreviewed"
            and not field.value.strip()
            and identity(field)[2] not in lock_policy["lock_required"]
            for field in candidates
        )
    page = Paginator(digital.order_by("key"), 30).get_page(request.GET.get("page", 1))
    ids = [f.pk for f in page.object_list]
    FormSet = modelformset_factory(Field, form=ReviewRowForm, extra=0)
    forms = FormSet(request.POST or None, queryset=job.fields.filter(pk__in=ids).order_by("key"))
    ajax_save = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    valid = forms.is_valid() if request.method == "POST" else False
    if request.method == "POST" and ajax_save and not valid:
        return JsonResponse({"saved": False, "errors": forms.errors}, status=400)
    if request.method == "POST" and valid:
        with transaction.atomic():
            job = Job.objects.select_for_update().get(pk=pk)
            if job_locked(job):
                return HttpResponse("Report data is locked for review or issuance.", status=409)
            for form in forms:
                if form.instance.pk not in ids:
                    return HttpResponse("Invalid review selection.", status=400)
                if not station_edit_allowed(request.user, job, identity(form.instance)[0]):
                    return HttpResponse("This reading is not open at your station.", status=403)
                if Field.objects.get(pk=form.instance.pk).version != form.cleaned_data["version"]:
                    return HttpResponse("A reading changed. Reload before saving.", status=409)
            changed = 0
            for form in forms:
                if form.has_changed():
                    field = form.save(commit=False)
                    before = Field.objects.get(pk=field.pk)
                    field.version = before.version + 1
                    field.updated_by = request.user
                    field.save()
                    changed += 1
                    AuditEvent.objects.create(
                        job=job,
                        actor=request.user,
                        action="reading_updated",
                        details={
                            "key": field.key,
                            "before": {
                                "value": before.value,
                                "unit": before.unit,
                                "status": before.status,
                            },
                            "after": {
                                "value": field.value,
                                "unit": field.unit,
                                "status": field.status,
                            },
                        },
                    )
            if changed:
                Job.objects.filter(pk=pk).update(version=F("version") + 1)
        if ajax_save:
            return JsonResponse(
                {
                    "saved": True,
                    "changed": changed,
                    "versions": {str(form.instance.pk): form.instance.version for form in forms},
                }
            )
        messages.success(request, f"Saved {changed} changed readings.")

        def page_url(number):
            return (
                request.path
                + "?"
                + urlencode(
                    {"test": selected_type, "page": number} if selected_type else {"page": number}
                )
            )

        if request.POST.get("action") == "next" and page.has_next():
            return redirect(page_url(page.next_page_number()))
        if request.POST.get("action") == "save":
            return redirect(page_url(page.number))
        return redirect("findings", pk=pk)
    from .quality import validation_policy

    live_rules = [
        rule
        for rule in validation_policy()["checks"]
        if rule["type"] in ("mean", "sum", "difference", "range", "unit")
        and rule.get("form_type") in (None, selected_type)
    ]
    return render(
        request,
        "lab/digital_review.html",
        {
            "job": job,
            "forms": forms,
            "page": page,
            "live_rules": live_rules,
            "selected_run": selected_run,
            "selected_type": selected_type,
            "total_readings": total,
            "remaining_readings": remaining,
            "lock_required": lock_policy["lock_required"] if lock_policy else [],
            "result_any_of": lock_policy["result_any_of"] if lock_policy else [],
            "ready_for_bulk": ready_for_bulk,
            "optional_blank_count": optional_blank_count,
            "lock_blockers": lock_blockers,
        },
    )


@login_required
def findings(request, pk):
    job = get_object_or_404(allowed_jobs(request.user), pk=pk)
    data = assemble(job)
    latest = job.insight_drafts.first()
    return render(
        request,
        "lab/findings.html",
        {
            "job": job,
            "data": data,
            "insight": latest,
            "insight_current": latest and latest.evidence_hash == data["evidence_hash"],
        },
    )


@login_required
@require_POST
def request_insights(request, pk):
    job = get_object_or_404(allowed_jobs(request.user), pk=pk)
    if not can_manage_job(request.user, job):
        return HttpResponse("Lab registration authority required.", status=403)
    if job_locked(job):
        return HttpResponse("Report data is locked for review or issuance.", status=409)
    from django.conf import settings

    if settings.INSIGHT_PROVIDER == "gemini" and not settings.PAID_AI_ALLOWED:
        messages.error(
            request,
            "Paid AI explanations are paused. The rule-based findings and reporting remain available.",
        )
        return redirect("findings", pk=pk)
    if settings.INSIGHT_PROVIDER == "gemini" and not (settings.DEBUG or settings.CLOUD_AI_ALLOWED):
        messages.error(
            request,
            "Cloud AI is disabled for production source data. Ask an administrator to approve and enable it.",
        )
        return redirect("findings", pk=pk)
    if settings.INSIGHT_PROVIDER == "gemini" and not getattr(settings, "GEMINI_API_KEY", ""):
        messages.error(
            request,
            "AI provider is not configured. Evidence checks and reporting remain available.",
        )
        return redirect("findings", pk=pk)
    with transaction.atomic():
        job = Job.objects.select_for_update().get(pk=pk)
        if job_locked(job):
            return HttpResponse("Report data is locked for review or issuance.", status=409)
        data = assemble(job)
        existing = job.insight_drafts.filter(
            evidence_hash=data["evidence_hash"],
            status__in=["queued", "processing", "ready", "approved"],
        ).first()
        if existing and existing.status in ("queued", "processing"):
            last_attempt = existing.processing_started_at or existing.created_at
            if last_attempt <= timezone.now() - timedelta(seconds=settings.AI_JOB_TIMEOUT + 60):
                existing.status = "queued"
                existing.processing_started_at = timezone.now()
                existing.processing_token = ""
                existing.save(update_fields=["status", "processing_started_at", "processing_token"])
                transaction.on_commit(lambda: async_task("lab.insights.generate", existing.pk))
        elif not existing:
            draft = InsightDraft.objects.create(job=job, evidence_hash=data["evidence_hash"])
            transaction.on_commit(lambda: async_task("lab.insights.generate", draft.pk))
    messages.info(
        request,
        "AI explanation requested. Refresh this page to see the result. Source data is not changed.",
    )
    return redirect("findings", pk=pk)


@login_required
@require_POST
def approve_insights(request, pk):
    with transaction.atomic():
        insight = get_object_or_404(
            InsightDraft.objects.filter(job__in=allowed_jobs(request.user)), pk=pk
        )
        current_job = Job.objects.select_for_update().get(pk=insight.job_id)
        if not can_manage_job(request.user, current_job):
            return HttpResponse("Lab registration authority required.", status=403)
        if job_locked(current_job):
            return HttpResponse("Report data is locked for review or issuance.", status=409)
        insight = InsightDraft.objects.select_for_update().get(pk=pk)
        if (
            insight.status != "ready"
            or insight.evidence_hash != assemble(current_job)["evidence_hash"]
        ):
            return HttpResponse(
                "This explanation is unavailable or out of date. Generate it again.", status=409
            )
        if request.POST.get("confirmed") != "yes":
            return HttpResponse(
                "Confirm that you checked the explanation against its evidence.", status=400
            )
        insight.status = "approved"
        insight.reviewed_by = request.user
        insight.reviewed_at = timezone.now()
        insight.save()
        AuditEvent.objects.create(
            job=insight.job,
            actor=request.user,
            action="insight_reviewed",
            details={"insight": insight.pk},
        )
    return redirect("findings", pk=insight.job_id)


@login_required
@permission_required("lab.approve_report", raise_exception=True)
@require_POST
def approve_report(request, pk):
    from .notifications import notify_customer
    from .report_workflow import current_snapshot, latest_report, make_issue_seal

    with transaction.atomic():
        obj = get_object_or_404(Report.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
        current_job = Job.objects.select_for_update().get(pk=obj.job_id)
        obj = Report.objects.select_for_update().get(pk=pk)
        if obj.approved_at:
            return redirect("report", pk=pk)
        if (
            not latest_report(current_job, obj)
            or not obj.engineer_locked_at
            or not obj.quality_verified_at
            or obj.returned_at
        ):
            return HttpResponse(
                "Engineer lock and independent quality verification are required first.", status=409
            )
        if request.user.pk in (obj.engineer_locked_by_id, obj.quality_verified_by_id):
            return HttpResponse(
                "The HoD signer must be distinct from engineer and quality reviewer.", status=403
            )
        current = current_snapshot(current_job, obj)
        if current is None:
            return HttpResponse(
                "Source data changed. Create and review a new report revision.", status=409
            )
        if current["blockers"]:
            return HttpResponse(
                "Resolve the report readiness blockers before approval.", status=400
            )
        if request.POST.get("confirmed") != "yes":
            return HttpResponse(
                "Confirm review of the report, warnings and applicable test scope.", status=400
            )
        from django.core.files.base import ContentFile

        from .pdf_export import render_pdf

        obj.approved_by = request.user
        obj.approved_at = timezone.now()
        obj.issue_seal = make_issue_seal(obj, request.user, obj.approved_at)
        from .pdf_signing import SigningConfigurationError, sign_issued_pdf

        unsigned_pdf = render_pdf(obj)
        from .pdf_text_check import verify_request_pdf_text

        try:
            pdf_mismatches = verify_request_pdf_text(unsigned_pdf, current_job.request_snapshot)
        except ValueError:
            return HttpResponse(
                "Issued PDF text could not be checked against the customer request.", status=409
            )
        if pdf_mismatches:
            return HttpResponse(
                "Issued PDF differs from the customer request: " + ", ".join(pdf_mismatches),
                status=409,
            )
        try:
            pdf_bytes, certificate_signed = sign_issued_pdf(
                unsigned_pdf, request.user.get_full_name() or request.user.get_username()
            )
        except SigningConfigurationError as error:
            return HttpResponse(
                str(error),
                status=503,
            )
        obj.approved_pdf.save(f"{obj.pk}-r{obj.revision}.pdf", ContentFile(pdf_bytes), save=False)
        obj.approved_pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        obj.save(
            update_fields=[
                "approved_by",
                "approved_at",
                "approved_pdf",
                "approved_pdf_sha256",
                "issue_seal",
            ]
        )
        AuditEvent.objects.create(
            job=obj.job,
            actor=request.user,
            action="report_approved",
            details={
                "report": str(obj.pk),
                "sha256": obj.snapshot_sha256,
                "issue_seal": obj.issue_seal,
                "certificate_signed": certificate_signed,
            },
        )
        notify_customer(
            obj.job,
            "report_issued",
            f'The approved report for {obj.job.file_number or "your request"} is available in your portal.',
        )
    messages.success(
        request,
        "Report signed and issued. The customer can download it in the portal; check delivery status before claiming email was sent.",
    )
    return redirect("report", pk=pk)


@login_required
def archive(request):
    from .search import HistoryFilter, pagination_query

    form = HistoryFilter(request.GET, reports=True)
    valid = form.is_valid()
    filters = form.cleaned_data if valid else {}
    query = filters.get("q", "")
    reports = Report.objects.filter(job__in=allowed_jobs(request.user)).select_related(
        "job", "approved_by"
    )
    if query:
        reports = reports.filter(
            Q(search_text__icontains=query)
            | Q(snapshot__customer__icontains=query)
            | Q(snapshot__sample_code__icontains=query)
            | Q(snapshot__test_series__icontains=query)
            | Q(snapshot__title__icontains=query)
        )
    for key, column in [
        ("customer", "snapshot__customer__icontains"),
        ("sample", "snapshot__sample_code__icontains"),
        ("date_from", "created_at__date__gte"),
        ("date_to", "created_at__date__lte"),
    ]:
        if filters.get(key):
            reports = reports.filter(**{column: filters[key]})
    if filters.get("status"):
        reports = reports.filter(approved_at__isnull=filters["status"] == "draft")
    if not valid:
        reports = reports.none()
    return render(
        request,
        "lab/archive.html",
        {
            "page": Paginator(reports.order_by("-created_at"), 25).get_page(
                request.GET.get("page", 1)
            ),
            "query": query,
            "filter_form": form,
            "filter_query": pagination_query(request),
        },
    )
