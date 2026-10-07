"""Named roles used by lab and customer routes. Unassigned production users get no lab access."""

from django.conf import settings

CUSTOMER = "Customer"
ENGINEER = "Test Engineer"
QUALITY = "Quality"
HOD = "HoD"
ADMIN = "Admin"
NAMES = (CUSTOMER, ENGINEER, QUALITY, HOD, ADMIN)


def role_for(user):
    if not user.is_authenticated:
        return ""
    if hasattr(user, "_vectorlab_role"):
        return user._vectorlab_role
    if user.is_superuser:
        user._vectorlab_role = ADMIN
        user._vectorlab_has_groups = False
        return ADMIN
    memberships = list(user.groups.values_list("name", flat=True))
    user._vectorlab_has_groups = bool(memberships)
    assigned = set(memberships) & set(NAMES)
    user._vectorlab_role = assigned.pop() if len(assigned) == 1 and len(memberships) == 1 else ""
    return user._vectorlab_role


def lab_access(user):
    role = role_for(user)
    if role in (ENGINEER, QUALITY, HOD, ADMIN):
        return True
    # Pre-existing local development accounts have no group assignment.
    # Never apply this compatibility path to a production deployment.
    return bool(
        settings.LOCAL_LEGACY_ACCESS
        and not role
        and user.is_authenticated
        and not user._vectorlab_has_groups
    )


def role_context(request):
    role = role_for(request.user)
    count = 0
    if request.user.is_authenticated:
        from .models import Report, TestRun

        if role == QUALITY:
            count = Report.objects.filter(
                engineer_locked_at__isnull=False,
                quality_verified_at__isnull=True,
                returned_at__isnull=True,
                approved_at__isnull=True,
            ).count()
        elif role == HOD:
            count = Report.objects.filter(
                quality_verified_at__isnull=False,
                returned_at__isnull=True,
                approved_at__isnull=True,
            ).count()
        elif role == ENGINEER:
            count = TestRun.objects.filter(
                assigned_to=request.user, status__in=["pending", "in_progress"]
            ).count()
    return {"lab_role": role, "lab_access": lab_access(request.user), "action_count": count}


def station_edit_allowed(user, job, test_type):
    """An assigned engineer edits only their station; local legacy owners remain supported."""
    from .models import TestRun

    if test_type and TestRun.objects.filter(job=job, test_type=test_type, status="locked").exists():
        return False
    role = role_for(user)
    if role == ADMIN:
        return True
    if role == ENGINEER:
        return bool(
            test_type
            and TestRun.objects.filter(job=job, test_type=test_type, assigned_to=user)
            .exclude(status="locked")
            .exists()
        )
    return bool(
        settings.LOCAL_LEGACY_ACCESS
        and not role
        and user.is_authenticated
        and user.pk == job.owner_id
    )


def can_manage_job(user, job):
    role = role_for(user)
    return role == ADMIN or bool(
        settings.LOCAL_LEGACY_ACCESS and not role and user.pk == job.owner_id
    )
