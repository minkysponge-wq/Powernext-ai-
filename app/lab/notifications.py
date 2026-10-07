"""Customer-facing in-app notices; email transport is a separate delivery gate."""

from .models import CustomerNotification


def notify_customer(job, kind, message):
    if not job.customer_user_id:
        return None
    notice, _ = CustomerNotification.objects.get_or_create(
        job=job, recipient_id=job.customer_user_id, kind=kind, defaults={"message": message[:300]}
    )
    return notice
