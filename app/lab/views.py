import hashlib
from io import BytesIO
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, F, Q, prefetch_related_objects
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django_q.tasks import async_task

from .forms import FieldForm, JobForm, UploadForm
from .models import AuditEvent, Document, Field, Job, Report, TestRun
from .report_workflow import job_locked
from .roles import (
    CUSTOMER,
    ENGINEER,
    HOD,
    QUALITY,
    can_manage_job,
    lab_access,
    role_for,
    station_edit_allowed,
)


def allowed_jobs(user):
    if not lab_access(user):
        return Job.objects.none()
    return (
        Job.objects.all()
        if user.is_superuser
        or user.has_perm("lab.approve_report")
        or user.has_perm("lab.verify_report")
        else Job.objects.filter(Q(owner=user) | Q(test_runs__assigned_to=user)).distinct()
    )


def progress_for(job, counts=None, has_documents=None):
    if counts is None:
        counts = job.fields.aggregate(
            total=Count("pk"),
            reviewed=Count("pk", filter=Q(status__in=["verified", "not_applicable"])),
        )
    job.field_count = counts["total"]
    job.reviewed_count = counts["reviewed"]
    job.pending_count = job.field_count - job.reviewed_count
    job.review_percent = round(100 * job.reviewed_count / job.field_count) if job.field_count else 0
    job.next_step = (
        "upload" if not job.field_count else "review" if job.pending_count else "results"
    )
    job.next_label = {
        "upload": "Upload forms",
        "review": "Review readings",
        "results": "Check results",
    }[job.next_step]
    if not job.field_count and (job.documents.exists() if has_documents is None else has_documents):
        job.next_step = "review"
        job.next_label = (
            "Retry extraction"
            if job.documents.filter(status="failed").exists()
            else "Extract readings"
        )
    prefetched = getattr(job, "_prefetched_objects_cache", {}).get("reports")
    latest = (
        (prefetched[0] if prefetched else None)
        if prefetched is not None
        else job.reports.order_by("-revision").first()
    )
    if latest and latest.approved_at:
        job.next_step, job.next_label = "export", "Issued report"
    elif latest and latest.quality_verified_at and not latest.returned_at:
        job.next_step, job.next_label = "export", "Awaiting HoD"
    elif latest and latest.engineer_locked_at and not latest.returned_at:
        job.next_step, job.next_label = "export", "Awaiting Quality"
    elif latest and latest.returned_at:
        job.next_step, job.next_label = "review", "Address corrections"
    elif latest and not job.pending_count:
        job.next_step, job.next_label = "export", "Review draft"
    return job


@login_required
def dashboard(request):
    if role_for(request.user) == CUSTOMER:
        return redirect("customer_home")
    if not lab_access(request.user):
        return HttpResponse("This account does not have laboratory-workspace access.", status=403)
    from django.core.paginator import Paginator
    from django.db.models import Exists, OuterRef, Subquery

    from .search import HistoryFilter, pagination_query

    jobs = allowed_jobs(request.user)
    form = HistoryFilter(request.GET)
    valid = form.is_valid()
    filters = form.cleaned_data if valid else {}
    query = filters.get("q", "")
    if query:
        report_jobs = []
        if query.upper().startswith("VL-SCL-"):
            from .verification_link import issued_report_number

            report_jobs = [
                report.job_id
                for report in Report.objects.filter(
                    job__in=jobs, approved_at__isnull=False
                ).only("id", "job_id", "approved_at", "snapshot")
                if query.casefold() in issued_report_number(report).casefold()
            ]
        jobs = jobs.filter(
            Q(file_number__icontains=query)
            | Q(title__icontains=query)
            | Q(customer__icontains=query)
            | Q(sample_code__icontains=query)
            | Q(test_series__icontains=query)
            | Q(pk__in=report_jobs)
        )
    jobs = jobs.annotate(
        has_readings=Exists(Field.objects.filter(job_id=OuterRef("pk"))),
        has_pending=Exists(
            Field.objects.filter(job_id=OuterRef("pk")).exclude(
                status__in=["verified", "not_applicable"]
            )
        ),
        latest_approval=Subquery(
            Report.objects.filter(job_id=OuterRef("pk"))
            .order_by("-revision")
            .values("approved_at")[:1]
        ),
    )
    for key, column in [
        ("customer", "customer__icontains"),
        ("sample", "sample_code__icontains"),
        ("date_from", "created_at__date__gte"),
        ("date_to", "created_at__date__lte"),
    ]:
        if filters.get(key):
            jobs = jobs.filter(**{column: filters[key]})
    status = filters.get("status")
    if status == "empty":
        jobs = jobs.filter(has_readings=False)
    elif status == "review":
        jobs = jobs.filter(has_pending=True)
    elif status == "checked":
        jobs = jobs.filter(has_readings=True, has_pending=False)
    elif status == "approved":
        jobs = jobs.filter(latest_approval__isnull=False)
    if not valid:
        jobs = jobs.none()
    totals = Field.objects.filter(job__in=jobs).aggregate(
        field_total=Count("pk"),
        reviewed_total=Count("pk", filter=Q(status__in=["verified", "not_applicable"])),
    )
    needs_review = jobs.filter(has_pending=True).count()
    recent_reports = (
        Report.objects.filter(job__in=jobs).select_related("job").order_by("-created_at")[:5]
    )
    job_page = Paginator(jobs, 25).get_page(request.GET.get("page", 1))
    visible_jobs = list(job_page)
    prefetch_related_objects(visible_jobs, "reports")
    ids = [job.pk for job in visible_jobs]
    page_counts = {
        row["job_id"]: {"total": row["total"], "reviewed": row["reviewed"]}
        for row in Field.objects.filter(job_id__in=ids)
        .values("job_id")
        .annotate(
            total=Count("pk"),
            reviewed=Count("pk", filter=Q(status__in=["verified", "not_applicable"])),
        )
    }
    empty_ids = [job.pk for job in visible_jobs if job.pk not in page_counts]
    with_documents = (
        set(Document.objects.filter(job_id__in=empty_ids).values_list("job_id", flat=True))
        if empty_ids
        else set()
    )
    jobs = [
        progress_for(
            job, page_counts.get(job.pk, {"total": 0, "reviewed": 0}), job.pk in with_documents
        )
        for job in visible_jobs
    ]
    my_tests = (
        (
            TestRun.objects.filter(assigned_to=request.user, status__in=["pending", "in_progress"])
            .select_related("job")
            .order_by("status", "job__created_at")[:12]
        )
        if role_for(request.user) == ENGINEER
        else []
    )
    return render(
        request,
        "lab/dashboard.html",
        {
            "jobs": jobs,
            "job_page": job_page,
            "query": query,
            "needs_review": needs_review,
            **totals,
            "filter_form": form,
            "filter_query": pagination_query(request),
            "recent_reports": recent_reports,
            "my_tests": my_tests,
        },
    )


@login_required
def new_job(request):
    if not lab_access(request.user) or role_for(request.user) in (CUSTOMER, QUALITY, HOD):
        return HttpResponse(
            "Only a test engineer or lab administrator can create a test job.", status=403
        )
    form = JobForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            job = form.save(commit=False)
            job.owner = request.user
            job.save()
            AuditEvent.objects.create(job=job, actor=request.user, action="job_created")
        return redirect("job_detail", pk=job.pk)
    return render(
        request,
        "lab/form.html",
        {"form": form, "title": "Create a test job", "button": "Create job"},
    )


@login_required
def job_detail(request, pk):
    job = progress_for(get_object_or_404(allowed_jobs(request.user), pk=pk))
    documents = list(
        job.documents.annotate(
            reading_count=Count("field"),
            reviewed_count=Count(
                "field", filter=Q(field__status__in=["verified", "not_applicable"])
            ),
        )
    )
    for doc in documents:
        doc.review_percent = (
            round(100 * doc.reviewed_count / doc.reading_count) if doc.reading_count else 0
        )
    active = request.GET.get("step", job.next_step)
    if active not in ("capture", "upload", "review", "results", "report", "export"):
        active = job.next_step
    return render(
        request,
        "lab/job.html",
        {
            "job": job,
            "upload_form": UploadForm(),
            "test_runs": job.test_runs.select_related("assigned_to").order_by("test_type"),
            "digital_fields": job.fields.filter(document__isnull=True),
            "documents": documents,
            "loss_documents": [d for d in documents if d.form_type == "loss_calculation"],
            "unresolved": job.pending_count,
            "active_step": active,
            "form_choices": UploadForm().fields["form_type"].choices[1:],
            "events": job.events.select_related("actor")[:10],
        },
    )


@login_required
@require_POST
def upload(request, pk):
    job = get_object_or_404(allowed_jobs(request.user), pk=pk)
    if job_locked(job):
        return HttpResponse("Report data is locked for review or issuance.", status=409)
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose a PDF to upload.")
        return redirect("job_detail", pk=pk)
    if not station_edit_allowed(request.user, job, form.cleaned_data["form_type"]):
        return HttpResponse("This source form is not assigned to your station.", status=403)
    from django.conf import settings

    if (
        form.cleaned_data["form_type"]
        and settings.EXTRACTION_PROVIDER == "gemini"
        and not settings.PAID_AI_ALLOWED
    ):
        messages.error(
            request,
            "Paid AI extraction is paused. The PDF was not uploaded or queued; use digital entry or ask an administrator to enable a budgeted run.",
        )
        return redirect("job_detail", pk=pk)
    if (
        form.cleaned_data["form_type"]
        and settings.EXTRACTION_PROVIDER == "gemini"
        and not (settings.DEBUG or settings.CLOUD_AI_ALLOWED)
    ):
        messages.error(
            request,
            "Cloud extraction is disabled for production source data. Ask an administrator to approve and enable it.",
        )
        return redirect("job_detail", pk=pk)
    incoming = form.cleaned_data["file"]
    if incoming.size > 20 * 1024 * 1024:
        messages.error(request, "Maximum PDF size is 20 MB.")
        return redirect("job_detail", pk=pk)
    content = incoming.read()
    try:
        from .pdf_safety import probe_pdf

        pages = probe_pdf(content)["pages"]
    except ValueError:
        messages.error(request, "Upload an unencrypted PDF with 1–30 pages.")
        return redirect("job_detail", pk=pk)
    digest = hashlib.sha256(content).hexdigest()
    incoming.seek(0)
    with transaction.atomic():
        job = Job.objects.select_for_update().get(pk=pk)
        if job_locked(job):
            return HttpResponse("Report data is locked for review or issuance.", status=409)
        doc = job.documents.filter(sha256=digest).first()
        if doc:
            AuditEvent.objects.create(
                job=job,
                actor=request.user,
                action="duplicate_upload",
                details={"document": str(doc.pk)},
            )
            messages.info(request, "This document is already attached. No duplicate was created.")
        else:
            doc = Document.objects.create(
                job=job,
                original_name=Path(incoming.name).name[:255],
                file=incoming,
                sha256=digest,
                page_count=pages,
                form_type=form.cleaned_data["form_type"],
            )
            AuditEvent.objects.create(
                job=job,
                actor=request.user,
                action="document_uploaded",
                details={"document": str(doc.pk), "sha256": digest},
            )
            transaction.on_commit(lambda: async_task("lab.tasks.inspect_document", str(doc.pk)))
            messages.success(
                request, "Source saved. Processing is queued; refresh to see progress."
            )
    return redirect("job_detail", pk=pk)


@login_required
def source(request, pk):
    doc = get_object_or_404(Document.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
    # Original PDFs are untrusted uploads. Serve them as downloads; the review
    # workspace uses rasterised page previews instead of embedding PDF content.
    response = FileResponse(
        doc.file.open("rb"),
        as_attachment=True,
        content_type="application/pdf",
        filename=doc.original_name,
    )
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def edit_field(request, pk, field_id=None):
    job = get_object_or_404(allowed_jobs(request.user), pk=pk)
    if request.method == "POST" and job_locked(job):
        return HttpResponse("Report data is locked for review or issuance.", status=409)
    field = get_object_or_404(job.fields, pk=field_id) if field_id else None
    if (
        request.method == "POST"
        and field is None
        and job.test_runs.filter(status="locked").exists()
    ):
        return HttpResponse(
            "Use the assigned station form; a completed test is already locked.", status=409
        )
    if request.method == "POST":
        from .quality import identity

        if not station_edit_allowed(request.user, job, identity(field)[0] if field else ""):
            return HttpResponse("This reading is not assigned to your station.", status=403)
    form = FieldForm(request.POST or None, instance=field, job=job, initial={"origin": "digital"})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            job = Job.objects.select_for_update().get(pk=pk)
            if job_locked(job):
                return HttpResponse("Report data is locked for review or issuance.", status=409)
            if field is None and job.test_runs.filter(status="locked").exists():
                return HttpResponse(
                    "Use the assigned station form; a completed test is already locked.", status=409
                )
            if field:
                current = Field.objects.get(pk=field.pk)
                if current.version != form.cleaned_data["version"]:
                    return HttpResponse(
                        "This reading changed since you opened it. Reload and review the latest value.",
                        status=409,
                    )
                before = {"value": current.value, "unit": current.unit, "status": current.status}
            else:
                before = None
                if job.fields.filter(key=form.cleaned_data["key"]).exists():
                    form.add_error("key", "This field already exists in the job.")
                    return render(
                        request,
                        "lab/form.html",
                        {"form": form, "title": "Add a reading", "button": "Save reading"},
                    )
            record = form.save(commit=False)
            record.job, record.updated_by = job, request.user
            record.version = current.version + 1 if field else 0
            if not field:
                record.raw_value = record.value
            record.save()
            Job.objects.filter(pk=pk).update(version=F("version") + 1)
            AuditEvent.objects.create(
                job=job,
                actor=request.user,
                action="reading_updated" if field else "reading_created",
                details={
                    "key": record.key,
                    "before": before,
                    "after": {"value": record.value, "unit": record.unit, "status": record.status},
                },
            )
        if request.GET.get("return") == "exceptions":
            return redirect("exception_review", pk=pk)
        return redirect("job_detail", pk=pk)
    return render(
        request,
        "lab/form.html",
        {
            "form": form,
            "title": "Review reading" if field else "Add a reading",
            "button": "Save reading",
            "reading": field,
        },
    )


@login_required
@require_POST
def make_report(request, pk):
    with transaction.atomic():
        job = get_object_or_404(allowed_jobs(request.user).select_for_update(), pk=pk)
        if not can_manage_job(request.user, job):
            return HttpResponse("Lab registration authority required.", status=403)
        if job_locked(job):
            return HttpResponse("Report data is locked for review or issuance.", status=409)
        from .report_builder import save_draft

        report_obj, _ = save_draft(job, request.user)
    return redirect("report", pk=report_obj.pk)


@login_required
def report(request, pk):
    obj = get_object_or_404(Report.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
    from .report_workflow import issue_code

    response = render(
        request,
        "lab/report.html",
        {
            "report": obj,
            "data": obj.snapshot,
            "display_sections": obj.snapshot.get("report_sections")
            or obj.snapshot.get("sections", []),
            "verification_code": issue_code(obj) if obj.approved_at else "",
        },
    )
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def report_pdf(request, pk):
    from .pdf_export import render_pdf

    obj = get_object_or_404(Report.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
    provisional = request.GET.get("provisional") == "1"
    if provisional and (obj.approved_at or obj.approved_pdf):
        return HttpResponse("An issued report has no provisional preview.", status=409)
    if provisional:
        from types import SimpleNamespace

        from .provisional import preview_for_job

        if not can_manage_job(request.user, obj.job):
            return HttpResponse("Lab review authority required.", status=403)
        preview = SimpleNamespace(
            snapshot=preview_for_job(obj.job), approved_at=None, revision=obj.revision, pk=obj.pk
        )
        response = HttpResponse(render_pdf(preview), content_type="application/pdf")
    elif obj.approved_pdf:
        response = FileResponse(obj.approved_pdf.open("rb"), content_type="application/pdf")
    else:
        response = HttpResponse(render_pdf(obj), content_type="application/pdf")
    disposition = "inline" if request.GET.get("preview") == "1" or provisional else "attachment"
    response["Content-Disposition"] = (
        f'{disposition}; filename="report-{obj.pk}-r{obj.revision}.pdf"'
    )
    response["Cache-Control"] = "private, no-store"
    if disposition == "inline":
        response["X-Frame-Options"] = "SAMEORIGIN"
        response["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'self'"
    return response


@login_required
def report_html(request, pk):
    from .fixed_report import render_fixed_html

    obj = get_object_or_404(Report.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
    if ((obj.snapshot.get("template_mapping") or {}).get("definition") or {}).get(
        "format"
    ) != "CPRI-SCL-TR-v1":
        return HttpResponse("Printable HTML is available for CPRI-SCL-TR-v1 reports.", status=404)
    response = HttpResponse(render_fixed_html(obj), content_type="text/html; charset=utf-8")
    response["Cache-Control"] = "private, no-store"
    response["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return response


@login_required
def job_rules(request, pk):
    from .forms import JobRulesForm

    job = get_object_or_404(allowed_jobs(request.user), pk=pk)
    if request.method == "POST" and not can_manage_job(request.user, job):
        return HttpResponse("Lab registration authority required.", status=403)
    if request.method == "POST" and job_locked(job):
        return HttpResponse("Report data is locked for review or issuance.", status=409)
    form = JobRulesForm(request.POST or None, instance=job)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            current = Job.objects.select_for_update().get(pk=pk)
            if job_locked(current):
                return HttpResponse("Report data is locked for review or issuance.", status=409)
            if current.version != form.cleaned_data["version"]:
                return HttpResponse("Job changed. Reload before assigning rules.", status=409)
            current.rules.set(form.cleaned_data["rules"])
            Job.objects.filter(pk=pk).update(version=F("version") + 1)
            AuditEvent.objects.create(
                job=current,
                actor=request.user,
                action="rules_assigned",
                details={"rules": list(current.rules.values_list("id", flat=True))},
            )
        return redirect("job_detail", pk=pk)
    return render(
        request,
        "lab/form.html",
        {"form": form, "title": "Assign applicable rules", "button": "Save rules"},
    )


@login_required
@require_POST
def retry_extraction(request, pk):
    from .extraction import get_schema

    doc = get_object_or_404(Document.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
    if job_locked(doc.job):
        return HttpResponse("Report data is locked for review or issuance.", status=409)
    form_type = request.POST.get("form_type", doc.form_type)
    if not station_edit_allowed(request.user, doc.job, form_type):
        return HttpResponse("This source form is not assigned to your station.", status=403)
    try:
        schema = get_schema(form_type)
    except ValueError:
        return HttpResponse("Choose a supported form type.", status=400)
    if doc.page_count != schema["pages"]:
        return HttpResponse("Page count does not match this form.", status=400)
    from django.conf import settings

    if settings.EXTRACTION_PROVIDER == "gemini" and not settings.PAID_AI_ALLOWED:
        return HttpResponse("Paid AI extraction is paused. No retry was queued.", status=403)
    if settings.EXTRACTION_PROVIDER == "gemini" and not (
        settings.DEBUG or settings.CLOUD_AI_ALLOWED
    ):
        return HttpResponse("Cloud extraction is disabled for production source data.", status=403)
    with transaction.atomic():
        job = Job.objects.select_for_update().get(pk=doc.job_id)
        if job_locked(job):
            return HttpResponse("Report data is locked for review or issuance.", status=409)
        current = Document.objects.select_for_update().get(pk=doc.pk)
        if current.extraction_data:
            messages.info(
                request,
                "This document is already extracted. Existing readings have been preserved.",
            )
        else:
            current.form_type, current.status = form_type, "queued"
            current.processing_note = "Extraction queued."
            current.save(update_fields=["form_type", "status", "processing_note"])
            transaction.on_commit(lambda: async_task("lab.tasks.inspect_document", str(doc.pk)))
    return redirect("job_detail", pk=doc.job_id)


@login_required
def field_crop(request, pk):
    from PIL import Image

    from .extraction.rendering import render_review_page

    field = get_object_or_404(Field.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
    if not field.document or not field.bbox or not field.page:
        return HttpResponse("No source region recorded.", status=404)
    try:
        image = Image.open(BytesIO(render_review_page(field.document.file.path, field.page)))
    except (ValueError, OSError):
        return HttpResponse(
            "Source preview is unavailable. Use the original PDF download.", status=422
        )
    left, top, right, bottom = field.bbox
    w, h = image.size
    # A little context around the detected writing helps a reviewer interpret it.
    crop = image.crop(
        (
            max(0, int(left * w) - 18),
            max(0, int(top * h) - 18),
            min(w, int(right * w) + 18),
            min(h, int(bottom * h) + 18),
        )
    )
    stream = BytesIO()
    crop.save(stream, format="PNG")
    response = HttpResponse(stream.getvalue(), content_type="image/png")
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def review_document(request, pk):
    from django.core.paginator import Paginator
    from django.forms import modelformset_factory

    from .forms import ReviewRowForm
    from .quality import assess

    doc = get_object_or_404(Document.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
    if request.method == "POST" and not station_edit_allowed(request.user, doc.job, doc.form_type):
        return HttpResponse("This document is not assigned to your station.", status=403)
    if request.method == "POST" and job_locked(doc.job):
        return HttpResponse("Report data is locked for review or issuance.", status=409)
    page_number = request.GET.get("page", 1)
    paginator = Paginator(doc.field_set.order_by("page", "pk"), 20)
    page = paginator.get_page(page_number)
    ids = [field.pk for field in page.object_list]
    queryset = Field.objects.filter(pk__in=ids, document=doc).order_by("page", "pk")
    FormSet = modelformset_factory(Field, form=ReviewRowForm, extra=0)
    forms = FormSet(request.POST or None, queryset=queryset)
    checks = assess(list(doc.job.fields.select_related("document")))
    for form in forms:
        check = checks[form.instance.pk]
        reasons = check["failed"] + [
            item for item in check["issues"] if item not in check["failed"]
        ]
        form.review_reasons = (
            reasons[:2] if form.instance.status not in ("verified", "not_applicable") else []
        )
        form.more_review_reasons = max(0, len(reasons) - 2)
    ajax_save = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    valid = forms.is_valid() if request.method == "POST" else False
    if request.method == "POST" and ajax_save and not valid:
        return JsonResponse({"saved": False, "errors": forms.errors}, status=400)
    if request.method == "POST" and valid:
        with transaction.atomic():
            Job.objects.select_for_update().get(pk=doc.job_id)
            for form in forms:
                field = form.instance
                if field.pk not in ids:
                    return HttpResponse("Reading is outside this review page.", status=400)
                current = Field.objects.get(pk=field.pk)
                if current.version != form.cleaned_data["version"]:
                    return HttpResponse(
                        "A reading changed. Reload this review page before saving.", status=409
                    )
            changed = 0
            for form in forms:
                if form.has_changed():
                    field = form.save(commit=False)
                    current = Field.objects.get(pk=field.pk)
                    before = {
                        "value": current.value,
                        "unit": current.unit,
                        "status": current.status,
                    }
                    field.version = current.version + 1
                    field.updated_by = request.user
                    field.save()
                    AuditEvent.objects.create(
                        job=doc.job,
                        actor=request.user,
                        action="reading_updated",
                        details={
                            "key": field.key,
                            "before": before,
                            "after": {
                                "value": field.value,
                                "unit": field.unit,
                                "status": field.status,
                            },
                        },
                    )
                    changed += 1
            if changed:
                Job.objects.filter(pk=doc.job_id).update(version=F("version") + 1)
        if ajax_save:
            return JsonResponse(
                {
                    "saved": True,
                    "changed": changed,
                    "versions": {str(form.instance.pk): form.instance.version for form in forms},
                }
            )
        messages.success(request, f"Saved {changed} changed readings.")
        if request.POST.get("action") == "next":
            if page.has_next():
                return redirect(request.path + "?page=" + str(page.next_page_number()))
            from django.urls import reverse

            return redirect(reverse("job_detail", args=[doc.job_id]) + "?step=review")
        return redirect(request.path + "?page=" + str(page.number))
    return render(
        request,
        "lab/review.html",
        {
            "document": doc,
            "forms": forms,
            "page": page,
            "source_page": page.object_list[0].page if ids else 1,
        },
    )


@login_required
def transformer_results(request, pk):
    doc = get_object_or_404(
        Document.objects.filter(job__in=allowed_jobs(request.user), form_type="loss_calculation"),
        pk=pk,
    )
    from .reviewed_calculations import transformer_rows

    rows = []
    error = ""
    try:
        rows = transformer_rows(doc)
    except (ValueError, KeyError, ZeroDivisionError) as exc:
        error = str(exc)
    return render(request, "lab/transformer.html", {"document": doc, "rows": rows, "error": error})


@login_required
def source_page(request, pk, page):
    from .extraction.rendering import render_review_page

    doc = get_object_or_404(Document.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
    if not 1 <= page <= doc.page_count:
        return HttpResponse("Page not found.", status=404)
    try:
        png = render_review_page(doc.file.path, page)
    except (ValueError, OSError):
        return HttpResponse(
            "Source preview is unavailable. Use the original PDF download.", status=422
        )
    response = HttpResponse(png, content_type="image/png")
    response["Cache-Control"] = "private, no-store"
    return response
