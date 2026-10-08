"""Report-stage rules; a returned report stays in history while a new draft can be made."""

import hashlib
import hmac

from django.contrib.auth.decorators import login_required, permission_required
from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .assembly import assemble
from .models import AuditEvent, Job, Report


def allowed_jobs(user):
    from .views import allowed_jobs as query

    return query(user)


def job_locked(job):
    return job.reports.filter(
        Q(approved_at__isnull=False) | Q(engineer_locked_at__isnull=False, returned_at__isnull=True)
    ).exists()


def latest_report(job, report):
    newest = job.reports.order_by("-revision").values_list("pk", flat=True).first()
    return newest == report.pk


def current_snapshot(job, report):
    data = assemble(job)
    return data if report.snapshot.get("evidence_hash") == data["evidence_hash"] else None


@login_required
@permission_required("lab.lock_report", raise_exception=True)
@require_POST
def engineer_lock(request, pk):
    with transaction.atomic():
        report = get_object_or_404(Report.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
        if request.POST.get("confirmed") != "yes":
            return HttpResponse("Confirm every reading and source before locking.", status=400)
        job = Job.objects.select_for_update().get(pk=report.job_id)
        report = Report.objects.select_for_update().get(pk=pk)
        if not latest_report(job, report) or report.returned_at or job_locked(job):
            return HttpResponse(
                "This report cannot be locked. Create a current draft or check its stage.",
                status=409,
            )
        if job.documents.filter(status="queued").exists():
            return HttpResponse("Wait for queued source processing to finish.", status=409)
        if job.test_runs.exclude(status="locked").exists():
            return HttpResponse(
                "All assigned station tests must be locked before the report can be locked.",
                status=400,
            )
        current = current_snapshot(job, report)
        if current is None:
            return HttpResponse("Source data changed. Create a new report revision.", status=409)
        if current["blockers"]:
            return HttpResponse("Resolve readiness blockers before engineer lock.", status=400)
        report.engineer_locked_by = request.user
        report.engineer_locked_at = timezone.now()
        report.save(update_fields=["engineer_locked_by", "engineer_locked_at"])
        AuditEvent.objects.create(
            job=job,
            actor=request.user,
            action="engineer_locked",
            details={"report": str(report.pk), "snapshot_sha256": report.snapshot_sha256},
        )
    return redirect("report", pk=pk)


@login_required
@permission_required("lab.verify_report", raise_exception=True)
@require_POST
def quality_verify(request, pk):
    with transaction.atomic():
        report = get_object_or_404(Report.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
        if request.POST.get("confirmed") != "yes":
            return HttpResponse(
                "Confirm source, customer details and test scope were checked.", status=400
            )
        job = Job.objects.select_for_update().get(pk=report.job_id)
        report = Report.objects.select_for_update().get(pk=pk)
        if (
            not latest_report(job, report)
            or not report.engineer_locked_at
            or report.returned_at
            or report.quality_verified_at
            or report.approved_at
        ):
            return HttpResponse("Report is not awaiting quality verification.", status=409)
        if report.engineer_locked_by_id == request.user.pk:
            return HttpResponse(
                "The engineer who locked the report cannot quality-verify it.", status=403
            )
        if current_snapshot(job, report) is None:
            return HttpResponse("Source data changed after engineer lock.", status=409)
        report.quality_verified_by = request.user
        report.quality_verified_at = timezone.now()
        report.save(update_fields=["quality_verified_by", "quality_verified_at"])
        AuditEvent.objects.create(
            job=job,
            actor=request.user,
            action="quality_verified",
            details={"report": str(report.pk), "snapshot_sha256": report.snapshot_sha256},
        )
    return redirect("report", pk=pk)


@login_required
@require_POST
def return_for_correction(request, pk):
    with transaction.atomic():
        report = get_object_or_404(Report.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
        reason = request.POST.get("reason", "").strip()
        code = request.POST.get("reason_code", "").strip()
        if code:
            labels = {
                "request_mismatch": "Customer details differ",
                "reading": "Reading needs correction",
                "rule": "Engineering check needs review",
                "format": "Report format needs correction",
                "other": "Other correction",
            }
            if code not in labels:
                return HttpResponse("Choose a listed correction reason.", status=400)
            note = request.POST.get("reason_note", "").strip()
            reason = labels[code] + (": " + note if note else "")
        if not reason or len(reason) > 500:
            return HttpResponse(
                "Give a concise correction reason (up to 500 characters).", status=400
            )
        job = Job.objects.select_for_update().get(pk=report.job_id)
        report = Report.objects.select_for_update().get(pk=pk)
        if (
            not latest_report(job, report)
            or not report.engineer_locked_at
            or report.returned_at
            or report.approved_at
        ):
            return HttpResponse("This report cannot be returned at its current stage.", status=409)
        permitted = (
            request.user.has_perm("lab.verify_report")
            if not report.quality_verified_at
            else request.user.has_perm("lab.approve_report")
        )
        if not permitted:
            return HttpResponse("This review stage requires another authorised user.", status=403)
        if request.user.pk in (report.engineer_locked_by_id, report.quality_verified_by_id):
            return HttpResponse(
                "The same person cannot perform consecutive approval stages.", status=403
            )
        report.returned_by = request.user
        report.returned_at = timezone.now()
        report.return_reason = reason
        report.save(update_fields=["returned_by", "returned_at", "return_reason"])
        AuditEvent.objects.create(
            job=job,
            actor=request.user,
            action="report_returned",
            details={
                "report": str(report.pk),
                "reason": reason,
                "snapshot_sha256": report.snapshot_sha256,
            },
        )
    return redirect("report", pk=pk)


def make_issue_seal(report, signer, when):
    """Application HMAC attestation; not a certificate-backed legal digital signature."""
    payload = f"{report.pk}:{report.snapshot_sha256}:{signer.pk}:{when.isoformat()}"
    return signing.Signer(salt="vectorlab-report-issue-v1").sign(payload)


def verify_issue_seal(report):
    if not report.issue_seal or not report.approved_by_id or not report.approved_at:
        return False
    try:
        payload = signing.Signer(salt="vectorlab-report-issue-v1").unsign(report.issue_seal)
    except signing.BadSignature:
        return False
    expected = f"{report.pk}:{report.snapshot_sha256}:{report.approved_by_id}:{report.approved_at.isoformat()}"
    return payload == expected


def issue_code(report):
    return hashlib.sha256(report.issue_seal.encode()).hexdigest()[:24] if report.issue_seal else ""


def uploaded_pdf_matches_issue(report, uploaded):
    """Compare a submitted PDF to the frozen issued-byte hash without storing it."""
    if not report.approved_pdf_sha256:
        return False
    digest = hashlib.sha256()
    for chunk in uploaded.chunks():
        digest.update(chunk)
    return hmac.compare_digest(digest.hexdigest(), report.approved_pdf_sha256)


@require_http_methods(["GET", "POST"])
def verify_issued(request, pk, code):
    """Show QR-linked issue facts and optionally compare a submitted PDF's bytes."""
    from .verification_link import issued_report_number

    report = get_object_or_404(
        Report.objects.select_related("approved_by", "engineer_locked_by", "quality_verified_by"),
        pk=pk,
    )
    if (
        not report.approved_at
        or not verify_issue_seal(report)
        or not hmac.compare_digest(issue_code(report), code)
    ):
        return HttpResponse("Application seal could not be verified.", status=404)
    uploaded = request.FILES.get("pdf") if request.method == "POST" else None
    matched = uploaded_pdf_matches_issue(report, uploaded) if uploaded else None
    return render(
        request,
        "lab/verify_report.html",
        {
            "report": report,
            "verification_code": code,
            "sample_code": report.snapshot.get("sample_code", ""),
            "report_number": issued_report_number(report),
            "matched": matched,
            "upload_attempted": request.method == "POST",
        },
    )
