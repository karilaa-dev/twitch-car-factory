from django.db import migrations


def disable_autostart_new_accounts(apps, schema_editor):
    FarmConfiguration = apps.get_model("controller", "FarmConfiguration")
    FarmConfiguration.objects.filter(autostart_new_accounts=True).update(
        autostart_new_accounts=False
    )


class Migration(migrations.Migration):

    dependencies = [
        ("controller", "0005_minerinstancestate_online_channels"),
    ]

    operations = [
        migrations.RunPython(
            disable_autostart_new_accounts,
            migrations.RunPython.noop,
        ),
    ]
