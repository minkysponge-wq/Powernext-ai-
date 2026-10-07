import uuid

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import AuditEvent, CustomerRequestDraft, Job, Report, TestRun
from .roles import CUSTOMER, role_for
from .test_catalog import station_types


class CustomerRequestForm(forms.Form):
    customer = forms.CharField(max_length=200, label="Customer organisation")
    customer_address = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 3}))
    manufacturer = forms.CharField(max_length=200)
    sample_particulars = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 3}))
    requested_tests = forms.MultipleChoiceField(widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["requested_tests"].choices = [
            (x["form_type"], x["label"]) for x in station_types()
        ]

    def clean(self):
        data = super().clean()
        # Trim surrounding paste whitespace only; preserve legal names and punctuation.
        for key in ("customer", "customer_address", "manufacturer", "sample_particulars"):
            if key in data:
                data[key] = data[key].strip()
        return data


@login_required
def home(request):
    if role_for(request.user) != CUSTOMER:
        return HttpResponse("Customer access only.", status=403)
    jobs = (
        Job.objects.filter(customer_user=request.user)
        .annotate(
            test_total=Count("test_runs", distinct=True),
            test_complete=Count("test_runs", filter=Q(test_runs__status="locked"), distinct=True),
            has_issued_report=Exists(
                Report.objects.filter(job_id=OuterRef("pk"), approved_at__isnull=False)
            ),
        )
        .order_by("-created_at")
    )
    query = request.GET.get("q", "").strip()[:100]
    if query:
        jobs = jobs.filter(
            Q(file_number__icontains=query)
            | Q(customer__icontains=query)
            | Q(sample_code__icontains=query)
            | Q(sample_particulars__icontains=query)
        )
    return render(
        request,
        "lab/customer_home.html",
        {"jobs": Paginator(jobs, 20).get_page(request.GET.get("page")), "query": query},
    )


@login_required
def new_request(request):
    if role_for(request.user) != CUSTOMER:
        return HttpResponse("Customer access only.", status=403)
    draft = CustomerRequestDraft.objects.filter(customer_user=request.user).first()
    form = CustomerRequestForm(
        request.POST or None, initial=draft.data if draft and request.method != "POST" else None
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            file_number = (
                "OV-" + timezone.now().strftime("%Y") + "-" + uuid.uuid4().hex[:12].upper()
            )
            job = Job.objects.create(
                owner=request.user,
                customer_user=request.user,
                file_number=file_number,
                title="Customer test request " + file_number,
                customer=data["customer"],
                customer_address=data["customer_address"],
                manufacturer=data["manufacturer"],
                sample_particulars=data["sample_particulars"],
                requested_tests=data["requested_tests"],
                request_snapshot={
                    key: data[key]
                    for key in (
                        "customer",
                        "customer_address",
                        "manufacturer",
                        "sample_particulars",
                        "requested_tests",
                    )
                },
                report_scope=data["requested_tests"],
                request_submitted_at=timezone.now(),
            )
            TestRun.objects.bulk_create(
                [TestRun(job=job, test_type=kind) for kind in data["requested_tests"]]
            )
            AuditEvent.objects.create(
                job=job,
                actor=request.user,
                action="customer_request_submitted",
                details={"file_number": file_number, "requested_tests": data["requested_tests"]},
            )
            CustomerRequestDraft.objects.filter(customer_user=request.user).delete()
        return redirect("customer_status", file_number=file_number)
    previous = (
        Job.objects.filter(customer_user=request.user, request_submitted_at__isnull=False)
        .order_by("-request_submitted_at")
        .first()
    )
    previous_details = (
        {key: getattr(previous, key) for key in ("customer", "customer_address", "manufacturer")}
        if previous
        else None
    )
    return render(
        request,
        "lab/customer_request.html",
        {"form": form, "draft": draft, "previous_details": previous_details},
    )


@login_required
@require_POST
def save_request_draft(request):
    if role_for(request.user) != CUSTOMER:
        return HttpResponse("Customer access only.", status=403)
    allowed = ("customer", "customer_address", "manufacturer", "sample_particulars")
    data = {key: request.POST.get(key, "")[:1000] for key in allowed}
    allowed_tests = {item["form_type"] for item in station_types()}
    data["requested_tests"] = list(
        dict.fromkeys(
            kind for kind in request.POST.getlist("requested_tests") if kind in allowed_tests
        )
    )
    CustomerRequestDraft.objects.update_or_create(
        customer_user=request.user, defaults={"data": data}
    )
    return JsonResponse({"saved": True})


@login_required
def status(request, file_number):
    if role_for(request.user) != CUSTOMER:
        return HttpResponse("Customer access only.", status=403)
    job = get_object_or_404(Job.objects.filter(customer_user=request.user), file_number=file_number)
    tests = list(job.test_runs.select_related("assigned_to").order_by("test_type"))
    latest = job.reports.filter(approved_at__isnull=False).order_by("-revision").first()
    all_locked = bool(tests) and all(test.status == "locked" for test in tests)
    quality_done = job.reports.filter(
        quality_verified_at__isnull=False, returned_at__isnull=True
    ).exists()
    return render(
        request,
        "lab/customer_status.html",
        {
            "job": job,
            "tests": tests,
            "notices": job.customer_notifications.all(),
            "complete": sum(test.status == "locked" for test in tests),
            "report": latest,
            "all_locked": all_locked,
            "quality_done": quality_done,
        },
    )


@login_required
def download_report(request, file_number):
    if role_for(request.user) != CUSTOMER:
        return HttpResponse("Customer access only.", status=403)
    job = get_object_or_404(Job.objects.filter(customer_user=request.user), file_number=file_number)
    report = (
        Report.objects.filter(job=job, approved_at__isnull=False)
        .exclude(approved_pdf="")
        .order_by("-revision")
        .first()
    )
    if report is None:
        raise Http404("Approved report is not available yet.")
    response = FileResponse(
        report.approved_pdf.open("rb"),
        as_attachment=True,
        content_type="application/pdf",
        filename=f"{file_number}-report-r{report.revision}.pdf",
    )
    response["Cache-Control"] = "private, no-store"
    return response
