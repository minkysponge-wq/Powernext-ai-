from django.contrib import admin

from .models import (
    AuditEvent,
    CustomerNotification,
    Document,
    Field,
    Job,
    Report,
    ReportTemplate,
    Rule,
    TestRun,
)


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    list_display = ["code", "version", "title", "status"]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReportTemplate)
class ReportTemplateAdmin(admin.ModelAdmin):
    list_display = ["name", "version", "active"]


@admin.register(Job, Document, Field, Report, AuditEvent, TestRun, CustomerNotification)
class ReadOnlyRecordAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
