"""Prepare private email drafts; actual transmission remains with laboratory staff."""

import hashlib
from email.message import EmailMessage
from email.policy import SMTP
from io import BytesIO

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.mail import EmailMessage as DjangoEmailMessage
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from pyhanko.pdf_utils.reader import PdfFileReader

from .models import AuditEvent, Report
from .roles import ADMIN, HOD, role_for
from .views import allowed_jobs


class DeliveryForm(forms.Form):
    recipient = forms.EmailField(label="Customer email")
    status = forms.ChoiceField(
        choices=[
            ("pending", "Not sent"),
            ("sent", "Sent by staff"),
            ("acknowledged", "Customer acknowledged"),
        ]
    )
    note = forms.CharField(
        max_length=500,
        required=False,
        label="Delivery or acknowledgement reference",
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    version = forms.IntegerField(widget=forms.HiddenInput)


@login_required
def delivery(request, pk):
    report = get_object_or_404(Report.objects.filter(job__in=allowed_jobs(request.user)), pk=pk)
    role = role_for(request.user)
    authorised = (
        role in (HOD, ADMIN)
        or request.user.has_perm("lab.approve_report")
        or (settings.LOCAL_LEGACY_ACCESS and not role and request.user.pk == report.job.owner_id)
    )
    if not authorised:
        return HttpResponse("Report delivery requires HoD or Admin authority.", status=403)
    if not report.approved_at or not report.approved_pdf:
        return HttpResponse(
            "Approve this report revision before preparing customer delivery.", status=400
        )
    form = DeliveryForm(
        request.POST or None,
        initial={
            "recipient": report.delivery_recipient,
            "status": report.delivery_status,
            "note": report.delivery_note,
            "version": report.delivery_version,
        },
    )
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data
        if request.POST.get("action") == "send_email":
            if not settings.EMAIL_DELIVERY_ENABLED or not all(
                (settings.EMAIL_HOST, settings.DEFAULT_FROM_EMAIL)
            ):
                return HttpResponse("SMTP delivery is not configured and enabled.", status=503)
            if request.POST.get("confirmed") != "yes":
                form.add_error(
                    None, "Confirm the recipient and approved signed PDF before sending."
                )
            elif (
                report.job.customer_user_id
                and values["recipient"].casefold() != report.job.customer_user.email.casefold()
            ):
                form.add_error(
                    "recipient", "Recipient must match the registered customer account email."
                )
            elif not report.job.customer_user_id and request.POST.get("recipient_checked") != "yes":
                form.add_error(
                    "recipient",
                    "Confirm this recipient against the customer request before sending.",
                )
            else:
                with report.approved_pdf.open("rb") as stream:
                    pdf_bytes = stream.read()
                if (
                    not report.approved_pdf_sha256
                    or hashlib.sha256(pdf_bytes).hexdigest() != report.approved_pdf_sha256
                ):
                    form.add_error(
                        None, "Approved PDF does not match its frozen issue hash; do not send it."
                    )
                    signed = False
                else:
                    try:
                        signed = bool(PdfFileReader(BytesIO(pdf_bytes)).embedded_signatures)
                    except Exception:
                        signed = False
                if not signed:
                    if not form.errors:
                        form.add_error(
                            None, "Automatic email requires a certificate-signed issued PDF."
                        )
                else:
                    with transaction.atomic():
                        current = Report.objects.select_for_update().get(pk=report.pk)
                        if (
                            current.delivery_version != values["version"]
                            or current.delivery_status != "pending"
                        ):
                            return HttpResponse(
                                "Delivery has changed or was already attempted. Reconcile before retry.",
                                status=409,
                            )
                        current.delivery_status = "sending"
                        current.delivery_recipient = values["recipient"]
                        current.delivery_version += 1
                        current.save(
                            update_fields=[
                                "delivery_status",
                                "delivery_recipient",
                                "delivery_version",
                            ]
                        )
                        AuditEvent.objects.create(
                            job=current.job,
                            actor=request.user,
                            action="delivery_send_started",
                            details={"report": str(current.pk), "recipient": values["recipient"]},
                        )
                    message = DjangoEmailMessage(
                        subject=f"Test report {report.snapshot.get('test_series','')} - revision {report.revision}".replace(
                            "\r", " "
                        ).replace(
                            "\n", " "
                        ),
                        body="Please find your approved laboratory report attached.\n\nFile number: "
                        + str(report.job.file_number or "")
                        + "\n",
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        to=[values["recipient"]],
                    )
                    message.attach(
                        f"report-{report.pk}-r{report.revision}.pdf", pdf_bytes, "application/pdf"
                    )
                    try:
                        delivered = message.send(fail_silently=False)
                    except Exception:
                        AuditEvent.objects.create(
                            job=report.job,
                            actor=request.user,
                            action="delivery_send_failed",
                            details={"report": str(report.pk), "recipient": values["recipient"]},
                        )
                        return HttpResponse(
                            "Email delivery failed. Status is held for reconciliation; do not resend until checked.",
                            status=503,
                        )
                    if delivered != 1:
                        AuditEvent.objects.create(
                            job=report.job,
                            actor=request.user,
                            action="delivery_send_uncertain",
                            details={"report": str(report.pk), "recipient": values["recipient"]},
                        )
                        return HttpResponse(
                            "Email outcome is uncertain. Reconcile before retry.", status=503
                        )
                    with transaction.atomic():
                        current = Report.objects.select_for_update().get(pk=report.pk)
                        current.delivery_status = "sent"
                        current.delivery_note = "Sent through configured SMTP transport."
                        current.delivery_version += 1
                        current.save(
                            update_fields=["delivery_status", "delivery_note", "delivery_version"]
                        )
                        AuditEvent.objects.create(
                            job=current.job,
                            actor=request.user,
                            action="delivery_email_sent",
                            details={"report": str(current.pk), "recipient": values["recipient"]},
                        )
                    return redirect("report_delivery", pk=pk)
            if form.errors:
                return render(
                    request,
                    "lab/delivery.html",
                    {
                        "report": report,
                        "form": form,
                        "events": [],
                        "smtp_enabled": settings.EMAIL_DELIVERY_ENABLED,
                    },
                    status=400,
                )
        if request.POST.get("action") == "email_draft":
            message = EmailMessage(policy=SMTP)
            message["To"] = values["recipient"]
            message["Subject"] = (
                f"Test report {report.snapshot.get('test_series','')} - revision {report.revision}".replace(
                    "\r", " "
                ).replace(
                    "\n", " "
                )
            )
            message["X-Unsent"] = "1"
            message.set_content(
                "Please find the approved laboratory report attached.\n\nSample: "
                + str(report.snapshot.get("sample_code", ""))
                + "\nPlease contact the laboratory with any questions.\n"
            )
            with report.approved_pdf.open("rb") as stream:
                message.add_attachment(
                    stream.read(),
                    maintype="application",
                    subtype="pdf",
                    filename=f"report-{report.pk}-r{report.revision}.pdf",
                )
            AuditEvent.objects.create(
                job=report.job,
                actor=request.user,
                action="delivery_draft_prepared",
                details={"report": str(report.pk), "recipient": values["recipient"], "sent": False},
            )
            response = HttpResponse(message.as_bytes(), content_type="message/rfc822")
            response["Content-Disposition"] = f'attachment; filename="report-{report.pk}.eml"'
            response["Cache-Control"] = "private, no-store"
            return response
        if (
            values["status"] in ("sent", "acknowledged") or report.delivery_status == "sending"
        ) and not values["note"].strip():
            form.add_error("note", "Record when/how it was sent or acknowledged.")
        elif request.POST.get("confirmed") != "yes":
            form.add_error(None, "Confirm this is an action that has actually taken place.")
        else:
            with transaction.atomic():
                current = Report.objects.select_for_update().get(pk=report.pk)
                if current.delivery_version != values["version"]:
                    return HttpResponse(
                        "Delivery record changed. Reload before saving.", status=409
                    )
                before = current.delivery_status
                current.delivery_status = values["status"]
                current.delivery_recipient = values["recipient"]
                current.delivery_note = values["note"]
                current.delivery_version += 1
                current.save(
                    update_fields=[
                        "delivery_status",
                        "delivery_recipient",
                        "delivery_note",
                        "delivery_version",
                    ]
                )
                AuditEvent.objects.create(
                    job=current.job,
                    actor=request.user,
                    action="delivery_status_recorded",
                    details={
                        "report": str(current.pk),
                        "before": before,
                        "status": values["status"],
                        "recipient": values["recipient"],
                        "reference": values["note"],
                    },
                )
            return redirect("report_delivery", pk=pk)
    events = report.job.events.filter(
        action__in=[
            "delivery_draft_prepared",
            "delivery_status_recorded",
            "delivery_send_started",
            "delivery_send_failed",
            "delivery_send_uncertain",
            "delivery_email_sent",
        ],
        details__report=str(report.pk),
    ).select_related("actor")[:20]
    return render(
        request,
        "lab/delivery.html",
        {
            "report": report,
            "form": form,
            "events": events,
            "smtp_enabled": settings.EMAIL_DELIVERY_ENABLED,
        },
    )
