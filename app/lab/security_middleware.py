"""Fail-closed second-factor gate for privileged sessions."""

import time
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse

from .roles import ADMIN, ENGINEER, HOD, QUALITY, role_for

PRIVILEGED_ROLES = frozenset((ENGINEER, HOD, QUALITY, ADMIN))


class PrivilegedIdleTimeoutMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and role_for(request.user) in PRIVILEGED_ROLES:
            now = time.time()
            last = request.session.get("privileged_last_activity")
            if last is not None and now - last > settings.PRIVILEGED_IDLE_SECONDS:
                logout(request)
                return redirect(reverse("login"))
            request.session["privileged_last_activity"] = now
        return self.get_response(request)


class PrivilegedMFAMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            settings.MFA_ENFORCED
            and request.user.is_authenticated
            and role_for(request.user) in PRIVILEGED_ROLES
            and not request.user.is_verified()
        ):
            allowed = (reverse("otp_verify"), reverse("logout"), reverse("login"))
            if request.path_info not in allowed:
                target = reverse("otp_verify") + "?" + urlencode({"next": request.get_full_path()})
                return redirect(target)
        return self.get_response(request)
