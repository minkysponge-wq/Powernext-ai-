"""Bounded batch intake; uncertain identity or form matches stay in triage."""

import hashlib
import re
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django_q.tasks import async_task

from .extraction import schemas
from .models import AuditEvent, Document, IntakeItem, Job
from .pdf_probe import MAX_PDF_BYTES
from .pdf_safety import probe_pdf
from .report_workflow import job_locked
from .roles import ADMIN, role_for

MAX_BATCH_FILES = 10
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
FORM_MARKERS = {
    "customer_request": ("customer request", "request for testing"),
    "work_instruction": ("work instruction",),
    "transformer_proforma": ("proforma for transformer", "transformer proforma"),
    "loss_measurement": ("losses measurement", "measurement of losses"),
    "loss_calculation": ("losses calculation", "calculation of losses"),
    "routine_test": ("routine test",),
    "short_circuit": ("short circuit test", "short-circuit test"),
    "temperature_rise": ("temperature rise", "temperature-rise"),
    "pressure_oil_leakage": ("oil leakage", "pressure test"),
}


def _admin(request):
    return role_for(request.user) == ADMIN


def _pdf_entries(uploaded):
    """Read bounded bytes; never extract archive paths to disk."""
    name = Path(uploaded.name).name
    if name.lower().endswith(".pdf"):
        if uploaded.size > MAX_PDF_BYTES:
            raise ValueError("A PDF exceeds the 20 MB limit.")
        return [(name, uploaded.read(MAX_PDF_BYTES + 1))]
    if not name.lower().endswith(".zip") or uploaded.size > MAX_ARCHIVE_BYTES:
        raise ValueError("Choose PDFs or a ZIP of at most 80 MB.")
    try:
        with ZipFile(BytesIO(uploaded.read(MAX_ARCHIVE_BYTES + 1))) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if not infos or len(infos) > MAX_BATCH_FILES:
                raise ValueError("The ZIP must contain 1–10 PDFs.")
            if any(
                "/" in info.filename
                or "\\" in info.filename
                or not info.filename.lower().endswith(".pdf")
                or info.flag_bits & 1
                or info.file_size > MAX_PDF_BYTES
                or (info.compress_size and info.file_size > 100 * info.compress_size)
                for info in infos
            ):
                raise ValueError("The ZIP contains an unsupported or oversized entry.")
            if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
                raise ValueError("The ZIP expands beyond the batch limit.")
            entries = []
            for info in infos:
                with archive.open(info) as source:
                    data = source.read(MAX_PDF_BYTES + 1)
                if len(data) > MAX_PDF_BYTES:
                    raise ValueError("A PDF in the ZIP exceeds the 20 MB limit.")
                entries.append((info.filename, data))
            return entries
    except BadZipFile as error:
        raise ValueError("The ZIP archive is invalid.") from error


def _identifier_in(text, value):
    value = str(value or "").strip()
    return (
        len(value) >= 4
        and re.search(r"(?<![\w])" + re.escape(value) + r"(?![\w])", text, flags=re.IGNORECASE)
        is not None
    )


def classify_text(text):
    normal = " ".join(text.casefold().split())
    matched = [
        name
        for name, markers in FORM_MARKERS.items()
        if any(marker in normal for marker in markers)
    ]
    form = matched[0] if len(matched) == 1 else ""
    jobs = [
        job
        for job in Job.objects.exclude(sample_code="")
        .exclude(test_series="")
        .only("id", "sample_code", "test_series")
        if _identifier_in(text, job.sample_code) and _identifier_in(text, job.test_series)
    ]
    job = jobs[0] if len(jobs) == 1 else None
    return job, form


def _extraction_enabled():
    if settings.EXTRACTION_PROVIDER == "cached_demo":
        return settings.DEBUG and settings.DEMO_EXTRACTION_CACHE is not None
    if settings.EXTRACTION_PROVIDER == "gemini":
        return settings.PAID_AI_ALLOWED and (settings.DEBUG or settings.CLOUD_AI_ALLOWED)
    return settings.EXTRACTION_PROVIDER == "local_qwen"


def route_item(item, job, form_type, actor=None):
    """Attach one validated staged PDF; preserve source and audit the decision."""
    if form_type and form_type not in {s["form_type"] for s in schemas()}:
        raise ValueError("Choose a configured form type.")
    with transaction.atomic():
        item = IntakeItem.objects.select_for_update().get(pk=item.pk)
        job = Job.objects.select_for_update().get(pk=job.pk)
        if item.status == "routed":
            return item.routed_document
        if item.status not in ("queued", "triage") or job_locked(job):
            raise ValueError("This batch item cannot be routed to the selected job.")
        with item.file.open("rb") as source:
            content = source.read(MAX_PDF_BYTES + 1)
        if hashlib.sha256(content).hexdigest() != item.sha256:
            raise ValueError("Staged PDF changed; route refused.")
        existing = Document.objects.filter(job=job, sha256=item.sha256).first()
        if existing:
            document = existing
        else:
            active = bool(form_type and _extraction_enabled())
            document = Document.objects.create(
                job=job,
                original_name=item.original_name,
                file=ContentFile(content, name=item.original_name),
                sha256=item.sha256,
                page_count=item.page_count,
                form_type=form_type,
                status="queued" if active else "ready",
                processing_note=(
                    "Extraction queued."
                    if active
                    else "Source stored. Extraction is paused or no form type was selected; review and start it when authorised."
                ),
            )
            if active:
                transaction.on_commit(
                    lambda pk=str(document.pk): async_task("lab.tasks.inspect_document", pk)
                )
        item.status = "routed"
        item.candidate_job = job
        item.candidate_form_type = form_type
        item.routed_document = document
        item.reason = (
            "Routed after identity and form review."
            if actor
            else "Exact text identifiers and unique form marker."
        )
        item.save(
            update_fields=[
                "status",
                "candidate_job",
                "candidate_form_type",
                "routed_document",
                "reason",
            ]
        )
        AuditEvent.objects.create(
            job=job,
            actor=actor,
            action="batch_document_routed",
            details={
                "document": str(document.pk),
                "intake_item": item.pk,
                "sha256": item.sha256,
                "form_type": form_type,
                "automatic": actor is None,
            },
        )
        return document


def classify_item(item_id):
    item = IntakeItem.objects.get(pk=item_id)
    if item.status != "queued":
        return item.status
    try:
        with item.file.open("rb") as source:
            parsed = probe_pdf(source.read(MAX_PDF_BYTES + 1), include_text=True)
        item.page_count = parsed["pages"]
        item.save(update_fields=["page_count"])
        job, form = classify_text(parsed["text"])
        if job and form and not job_locked(job):
            try:
                route_item(item, job, form)
                return "routed"
            except ValueError:
                # A job may lock after classification. Keep the source in
                # triage for an operator; it is not a corrupt PDF.
                pass
        item.candidate_job, item.candidate_form_type = job, form
        item.status = "triage"
        item.reason = "Confirm job and form: exact source-text identifiers or form marker were absent or ambiguous."
        item.save(update_fields=["candidate_job", "candidate_form_type", "status", "reason"])
        return "triage"
    except ValueError:
        IntakeItem.objects.filter(pk=item.pk, status="queued").update(
            status="failed",
            reason="Could not safely parse this PDF. Check the source and upload again.",
        )
        return "failed"


@login_required
def batch_intake(request):
    if not _admin(request):
        return HttpResponse("Administrator access required.", status=403)
    if request.method == "POST":
        uploads = request.FILES.getlist("files")
        if not uploads or len(uploads) > MAX_BATCH_FILES:
            messages.error(request, "Choose 1–10 PDFs or one ZIP.")
            return redirect("batch_intake")
        try:
            entries = [entry for uploaded in uploads for entry in _pdf_entries(uploaded)]
            if (
                not entries
                or len(entries) > MAX_BATCH_FILES
                or sum(len(data) for _, data in entries) > MAX_ARCHIVE_BYTES
            ):
                raise ValueError("The batch exceeds 10 PDFs or 80 MB.")
            if any(
                len(data) > MAX_PDF_BYTES
                or not data.startswith(b"%PDF-")
                or b"%%EOF" not in data[-2048:]
                for _, data in entries
            ):
                raise ValueError("Every entry must be a complete PDF of at most 20 MB.")
        except ValueError as error:
            messages.error(request, str(error))
            return redirect("batch_intake")
        ids = []
        with transaction.atomic():
            for name, data in entries:
                item = IntakeItem.objects.create(
                    file=ContentFile(data, name=Path(name).name),
                    original_name=Path(name).name[:255],
                    sha256=hashlib.sha256(data).hexdigest(),
                    page_count=0,
                    reason="Awaiting bounded PDF inspection.",
                    uploaded_by=request.user,
                )
                ids.append(item.pk)
            transaction.on_commit(
                lambda: [async_task("lab.intake.classify_item", pk) for pk in ids]
            )
        messages.success(
            request,
            f"{len(ids)} files saved. Classification is queued; uncertain matches will appear in triage.",
        )
        return redirect("batch_intake")
    query = request.GET.get("q", "").strip()[:100]
    available_jobs = Job.objects.order_by("-created_at")
    if query:
        available_jobs = available_jobs.filter(
            Q(file_number__icontains=query)
            | Q(sample_code__icontains=query)
            | Q(test_series__icontains=query)
            | Q(customer__icontains=query)
        )
    return render(
        request,
        "lab/batch_intake.html",
        {
            "items": IntakeItem.objects.select_related("candidate_job", "routed_document").order_by(
                "-created_at"
            )[:100],
            "jobs": available_jobs[:100],
            "query": query,
            "form_types": [
                (s["form_type"], s["form_type"].replace("_", " ").title()) for s in schemas()
            ],
        },
    )


@login_required
@require_POST
def triage_item(request, pk):
    if not _admin(request):
        return HttpResponse("Administrator access required.", status=403)
    item = get_object_or_404(IntakeItem, pk=pk, status="triage")
    job = get_object_or_404(Job, pk=request.POST.get("job_id"))
    try:
        route_item(item, job, request.POST.get("form_type", ""), request.user)
    except ValueError as error:
        messages.error(request, str(error))
    else:
        messages.success(
            request, "Document routed. Check its source and extraction status in the job."
        )
    return redirect("batch_intake")


@login_required
def intake_source(request, pk):
    if not _admin(request):
        return HttpResponse("Administrator access required.", status=403)
    item = get_object_or_404(IntakeItem, pk=pk)
    response = FileResponse(
        item.file.open("rb"),
        as_attachment=True,
        content_type="application/pdf",
        filename=item.original_name,
    )
    response["Cache-Control"] = "private, no-store"
    return response
