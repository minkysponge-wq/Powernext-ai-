from django.db import migrations


def create_roles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    report_type, _ = ContentType.objects.get_or_create(app_label="lab", model="report")
    for code, label in [
        ("lock_report", "Lock engineer data for quality review"),
        ("verify_report", "Verify a locked laboratory report as quality reviewer"),
        ("approve_report", "Approve a laboratory report for export as HoD"),
    ]:
        Permission.objects.get_or_create(
            content_type=report_type, codename=code, defaults={"name": label}
        )
    assignments = {
        "Customer": (),
        "Test Engineer": ("lock_report",),
        "Quality": ("verify_report",),
        "HoD": ("approve_report",),
        "Admin": ("lock_report", "verify_report", "approve_report"),
    }
    for name, codes in assignments.items():
        group, _ = Group.objects.get_or_create(name=name)
        group.permissions.set(
            Permission.objects.filter(content_type__app_label="lab", codename__in=codes)
        )


class Migration(migrations.Migration):
    dependencies = [
        ("lab", "0010_alter_report_options_report_engineer_locked_at_and_more"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]
    operations = [migrations.RunPython(create_roles, migrations.RunPython.noop)]
