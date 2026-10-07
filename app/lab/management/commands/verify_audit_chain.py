from django.core.management.base import BaseCommand, CommandError

from lab.audit_chain import verify_job
from lab.models import Job


class Command(BaseCommand):
    help = "Verify every per-job audit hash chain; exits nonzero on alteration."

    def handle(self, *args, **options):
        failures = []
        checked = 0
        for job in Job.objects.only("pk").iterator():
            checked += 1
            bad_event = verify_job(job)
            if bad_event is not None:
                failures.append(f"{job.pk}: event {bad_event}")
        if failures:
            raise CommandError("Audit chain mismatch: " + "; ".join(failures[:20]))
        self.stdout.write(self.style.SUCCESS(f"Verified audit chains for {checked} jobs."))
