"""OTP challenge; devices are provisioned by a trusted local administrator."""

import re

import django_otp
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from .roles import ADMIN, ENGINEER, HOD, QUALITY, role_for


@login_required
@require_http_methods(["GET", "POST"])
def otp_verify(request):
    if role_for(request.user) not in (ADMIN, ENGINEER, HOD, QUALITY):
        return redirect("dashboard")
    destination = request.POST.get("next") or request.GET.get("next") or reverse("dashboard")
    if not url_has_allowed_host_and_scheme(
        destination, {request.get_host()}, require_https=request.is_secure()
    ):
        destination = reverse("dashboard")
    if request.user.is_verified():
        return redirect(destination)
    error = ""
    if request.method == "POST":
        token = request.POST.get("token", "").strip()
        if not re.fullmatch(r"\d{6,8}", token):
            error = "Enter the code shown in your authenticator."
        else:
            for device in django_otp.devices_for_user(request.user, confirmed=True):
                verified = django_otp.verify_token(request.user, device.persistent_id, token)
                if verified:
                    django_otp.login(request, verified)
                    return redirect(destination)
            error = "The code was not accepted. Wait for a new code and try again."
    return render(
        request,
        "registration/otp_verify.html",
        {
            "error": error,
            "next": destination,
            "has_device": django_otp.user_has_device(request.user, confirmed=True),
        },
    )
