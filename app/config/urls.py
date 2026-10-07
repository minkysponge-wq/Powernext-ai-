from django.contrib import admin
from django.urls import include, path
from lab.mfa_views import otp_verify

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/otp/", otp_verify, name="otp_verify"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("lab.urls")),
]
