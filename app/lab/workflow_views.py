import csv
from collections import defaultdict

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.db.models import F
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .assembly import assemble
from .fixed_template import TEMPLATE_NAME, clone_with_binding
from .models import AuditEvent, Job, Report, ReportTemplate
from .quality import assess, identity, review_priority
from .report_workflow import job_locked
from .roles import can_manage_job, station_edit_allowed
from .views import allowed_jobs


def grouped_readings(job):
    fields = list(job.fields.select_related("document").order_by("page", "pk"))
    checks = assess(fields)
    groups = defaultdict(list)
    review_positions = defaultdict(int)
    for field in fields:
        kind, page, key = identity(field)
        group = key.split(".")[0] if "." in key else "General information"
        source = (
            field.document.original_name
            if field.document
            else field.context.get("source_reference", "Digital entry")
        )
        review_page = None
        if field.document_id:
            review_page = review_positions[field.document_id] // 20 + 1
            review_positions[field.document_id] += 1
        groups[(str(field.document_id or source), kind, page, group, source)].append(
            {
                "field": field,
                "priority": review_priority(field, checks[field.pk]),
                "review_page": review_page,
                **checks[field.pk],
            }
        )
    result = []
    for (source_id, kind, page, name, source), rows in groups.items():
        eligible = [r["field"].pk for r in rows if r["state"] == "automatically_checked"]
        token = (
            signing.dumps(
                {
                    "job": str(job.pk),
                    "version": job.version,
                    "fields": [
                        (r["field"].pk, r["field"].version)
                        for r in rows
                        if r["field"].pk in eligible
                    ],
                },
                salt="table-review",
            )
            if eligible
            else ""
        )
        result.append(
            {
                "name": name.replace("_", " ").title(),
                "kind": kind.replace("_", " ").title(),
                "source": source,
                "page": page,
                "rows": rows,
                "exceptions": [r for r in rows if r["state"] == "needs_review"],
                "priority_exceptions": [r for r in rows if r["priority"]],
                "checked": [r for r in rows if r["state"] == "automatically_checked"],
                "token": token,
            }
        )
    return result, checks


@login_required
def exception_review(request, pk):
    job = get_object_or_404(allowed_jobs(request.user), pk=pk)
    groups, checks = grouped_readings(job)
    totals = {
        state: sum(c["state"] == state for c in checks.values())
        for state in ("needs_review", "automatically_checked", "engineer_reviewed")
    }
    totals["priority"] = sum(len(group["priority_exceptions"]) for group in groups)
    focus_priority = request.GET.get("focus") == "priority"
    show_all = request.GET.get("view") == "all" and not focus_priority
    for group in groups:
        group["visible_rows"] = (
            group["priority_exceptions"]
            if focus_priority
            else group["rows"] if show_all else group["exceptions"]
        )
    return render(
        request,
        "lab/exceptions.html",
        {
            "job": job,
            "groups": groups,
            "totals": totals,
            "remaining_exceptions": totals["needs_review"] - totals["priority"],
            "show_all": show_all,
            "focus_priority": focus_priority,
            "active_step": "review",
        },
    )


@login_required
@require_POST
def approve_table(request, pk):
    with transaction.atomic():
        job = get_object_or_404(allowed_jobs(request.user).select_for_update(), pk=pk)
        if request.POST.get("confirmed") != "yes":
            return HttpResponse(
                "Confirm that you reviewed the displayed table against its sources.", status=400
            )
        try:
            token = signing.loads(request.POST.get("token", ""), salt="table-review", max_age=3600)
        except signing.BadSignature:
            return HttpResponse("This review selection expired. Reload the table.", status=409)
        if job_locked(job):
            return HttpResponse("Report data is locked for review or issuance.", status=409)
        if token.get("job") != str(pk) or token.get("version") != job.version:
            return HttpResponse("The job changed. Reload before approving.", status=409)
        fields = list(job.fields.select_related("document"))
        checks = assess(fields)
        by_id = {f.pk: f for f in fields}
        selected = token.get("fields", [])
        if not selected:
            return HttpResponse("No automatically checked readings selected.", status=400)
        for field_id, version in selected:
            if (
                field_id not in by_id
                or by_id[field_id].version != version
                or checks[field_id]["state"] != "automatically_checked"
            ):
                return HttpResponse("Readings or their check results changed. Reload.", status=409)
            if not station_edit_allowed(request.user, job, identity(by_id[field_id])[0]):
                return HttpResponse("This reading is not assigned to your station.", status=403)
        for field_id, _ in selected:
            f = by_id[field_id]
            f.status = "verified"
            f.version += 1
            f.updated_by = request.user
            f.save(update_fields=["status", "version", "updated_by", "updated_at"])
        Job.objects.filter(pk=pk).update(version=F("version") + 1)
        AuditEvent.objects.create(
            job=job,
            actor=request.user,
            action="table_reviewed",
            details={
                "field_ids": [x[0] for x in selected],
                "checks": {str(x[0]): checks[x[0]] for x in selected},
                "confirmation": "Engineer reviewed source table; not automatic verification",
            },
        )
    messages.success(
        request,
        f"{len(selected)} readings approved together. Exceptions still require individual review.",
    )
    return redirect("exception_review", pk=pk)


class MappingChoice(forms.Form):
    template = forms.ModelChoiceField(
        queryset=ReportTemplate.objects.none(),
        required=False,
        empty_label="Laboratory summary (default)",
    )
    version = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["template"].queryset = ReportTemplate.objects.filter(active=True)


@login_required
def report_mapping(request, pk):
    job = get_object_or_404(allowed_jobs(request.user), pk=pk)
    if request.method == "POST" and not can_manage_job(request.user, job):
        return HttpResponse("Lab registration authority required.", status=403)
    if request.method == "POST" and job_locked(job):
        return HttpResponse("Report data is locked for review or issuance.", status=409)
    form = MappingChoice(
        request.POST or None, initial={"template": job.report_template_id, "version": job.version}
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            current = Job.objects.select_for_update().get(pk=pk)
            if job_locked(current):
                return HttpResponse("Report data is locked for review or issuance.", status=409)
            if current.version != form.cleaned_data["version"]:
                return HttpResponse(
                    "Job changed. Reload before selecting its template.", status=409
                )
            current.report_template = form.cleaned_data["template"]
            current.version += 1
            current.save(update_fields=["report_template", "version"])
            AuditEvent.objects.create(
                job=current,
                actor=request.user,
                action="template_selected",
                details={"template": current.report_template_id},
            )
        return redirect("report_mapping", pk=pk)
    data = assemble(job)
    return render(
        request,
        "lab/mapping.html",
        {"job": job, "form": form, "data": data, "active_step": "mapping"},
    )


class FixedBindingForm(forms.Form):
    placeholder = forms.ChoiceField(label="Report placeholder")
    form_type = forms.ChoiceField(label="Source form")
    field_key = forms.CharField(label="Source field", max_length=160)
    unit = forms.CharField(label="Unit", max_length=30, required=False)
    decimals = forms.IntegerField(label="Decimal places", required=False, min_value=0, max_value=6)
    label = forms.CharField(label="Display label", max_length=160, required=False)
    job_version = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, definition, job_version, **kwargs):
        super().__init__(*args, **kwargs)
        from .extraction import schemas

        self.fields["placeholder"].choices = [
            (key, key.replace("_", " ").replace(".", " · "))
            for key, binding in definition["bindings"].items()
            if binding["source"] == "field"
        ]
        self.fields["form_type"].choices = [
            (schema["form_type"], schema["form_type"].replace("_", " ").title())
            for schema in schemas()
        ]
        self.fields["job_version"].initial = job_version
        if not self.is_bound:
            key = (
                "witness_name"
                if "witness_name" in definition["bindings"]
                else self.fields["placeholder"].choices[0][0]
            )
            current = definition["bindings"][key]
            self.initial.update(
                {
                    "placeholder": key,
                    "form_type": current["form_type"],
                    "field_key": current["key"],
                    "unit": current.get("unit", ""),
                    "decimals": current.get("decimals"),
                    "label": current.get("label", ""),
                    "job_version": job_version,
                }
            )


@login_required
def edit_fixed_mapping(request, pk):
    job = get_object_or_404(allowed_jobs(request.user), pk=pk)
    if not request.user.is_staff or not can_manage_job(request.user, job):
        return HttpResponse("Template administrator access required.", status=403)
    if job_locked(job):
        return HttpResponse("Report data is locked for review or issuance.", status=409)
    template = job.report_template
    if not template or template.name != TEMPLATE_NAME:
        return HttpResponse("Select CPRI-SCL-TR-v1 first.", status=400)
    form = FixedBindingForm(
        request.POST or None, definition=template.definition, job_version=job.version
    )
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data
        if values["job_version"] != job.version:
            return HttpResponse("Job changed. Reload mapping editor.", status=409)
        replacement = {
            "source": "field",
            "form_type": values["form_type"],
            "key": values["field_key"].strip(),
            "unit": values["unit"].strip(),
            "decimals": values["decimals"],
            "exact_copy": True,
            "label": values["label"].strip(),
        }
        try:
            definition = clone_with_binding(template.definition, values["placeholder"], replacement)
        except Exception as exc:
            form.add_error(None, str(exc))
        else:
            from .report_builder import save_draft

            with transaction.atomic():
                current = Job.objects.select_for_update().get(pk=pk)
                if job_locked(current) or current.version != values["job_version"]:
                    return HttpResponse("Job changed or locked. Reload mapping editor.", status=409)
                next_version = (
                    ReportTemplate.objects.filter(name=TEMPLATE_NAME)
                    .order_by("-version")
                    .values_list("version", flat=True)
                    .first()
                    or 0
                ) + 1
                updated = ReportTemplate(
                    name=TEMPLATE_NAME, version=next_version, definition=definition
                )
                updated.full_clean()
                updated.save()
                current.report_template = updated
                current.version += 1
                current.save(update_fields=["report_template", "version"])
                AuditEvent.objects.create(
                    job=current,
                    actor=request.user,
                    action="template_mapping_version_created",
                    details={
                        "template": updated.pk,
                        "version": next_version,
                        "placeholder": values["placeholder"],
                        "old": template.definition["bindings"][values["placeholder"]],
                        "new": replacement,
                    },
                )
                report, _ = save_draft(current, request.user, trigger="mapping_edit")
            return redirect("report_pdf", pk=report.pk)
    return render(
        request,
        "lab/fixed_mapping_editor.html",
        {"job": job, "template": template, "form": form, "active_step": "mapping"},
    )


@login_required
def report_evidence(request, pk):
    report = get_object_or_404(Report.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="report-{report.pk}-evidence.csv"'
    response["Cache-Control"] = "private, no-store"
    writer = csv.writer(response)
    writer.writerow(["Reading", "Section", "Page", "Field", "Value", "Unit", "Review", "Source"])

    def safe(value):
        value = str(value if value is not None else "")
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value

    for f in report.snapshot["fields"]:
        writer.writerow(
            [
                safe(v)
                for v in (
                    f["id"],
                    f.get("form_type"),
                    f.get("schema_page"),
                    f.get("schema_key", f["key"]),
                    f["value"],
                    f["unit"],
                    f["status"],
                    f.get("source"),
                )
            ]
        )
    return response
