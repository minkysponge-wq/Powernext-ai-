"""Quality and HoD queues keep review separate from station data entry."""

from collections import Counter

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render

from .models import AuditEvent, Report
from .roles import ADMIN, HOD, QUALITY, role_for
from .templatetags.labui import readable
from .views import allowed_jobs


@login_required
def queue(request):
    role = role_for(request.user)
    if role not in (QUALITY, HOD, ADMIN):
        return HttpResponse("Reviewer access required.", status=403)
    reports = Report.objects.filter(
        job__in=allowed_jobs(request.user),
        engineer_locked_at__isnull=False,
        returned_at__isnull=True,
        approved_at__isnull=True,
    ).select_related("job", "engineer_locked_by", "quality_verified_by")
    if role == QUALITY:
        reports = reports.filter(quality_verified_at__isnull=True)
    elif role == HOD:
        reports = reports.filter(quality_verified_at__isnull=False)
    return render(
        request,
        "lab/review_queue.html",
        {"reports": reports.order_by("created_at")[:100], "queue_role": role},
    )


@login_required
def quality_review(request, pk):
    if role_for(request.user) not in (QUALITY, HOD, ADMIN):
        return HttpResponse("Reviewer access required.", status=403)
    report = get_object_or_404(Report.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
    source = report.job.request_snapshot or {}
    names = (
        "customer",
        "customer_address",
        "manufacturer",
        "sample_particulars",
        "requested_tests",
    )

    def display(value):
        return ", ".join(readable(item) for item in value) if isinstance(value, list) else value

    comparisons = [
        {
            "label": name.replace("_", " ").title(),
            "source": display(source.get(name)),
            "report": display(report.snapshot.get(name)),
            "matches": source.get(name) == report.snapshot.get(name),
        }
        for name in names
        if name in source
    ]
    calculations = report.snapshot.get("calculations", [])
    attention = [
        result
        for result in calculations
        if result.get("verdict") in ("fail", "marginal", "blocked")
        or result.get("rule_status") != "confirmed"
    ]
    mismatch = [row for row in comparisons if not row["matches"]]
    na_reasons = Counter(
        detail.get("reason", "")
        for detail in report.job.events.filter(
            action="reading_marked_not_applicable", created_at__lte=report.created_at
        ).values_list("details", flat=True)
    )
    return render(
        request,
        "lab/quality_review.html",
        {
            "report": report,
            "comparisons": comparisons,
            "fields": report.snapshot.get("fields", []),
            "documents": report.job.documents.all(),
            "tests": report.job.test_runs.select_related("assigned_to").all(),
            "bulk_na_reasons": na_reasons.most_common(),
            "not_applicable_count": sum(
                field.get("status") == "not_applicable"
                for field in report.snapshot.get("fields", [])
            ),
            "attention": attention,
            "mismatch": mismatch,
        },
    )


@login_required
def audit_history(request, pk):
    job = get_object_or_404(allowed_jobs(request.user), pk=pk)
    events = AuditEvent.objects.filter(job=job).select_related("actor").order_by("-created_at")
    return render(
        request,
        "lab/audit_history.html",
        {"job": job, "page": Paginator(events, 100).get_page(request.GET.get("page", 1))},
    )
