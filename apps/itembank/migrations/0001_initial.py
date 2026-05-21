# Minimal baseline migration for the itembank app
# Generated on 2026-05-21 by assistant as a clean starting baseline.

from django.db import migrations


def noop_forward(apps, schema_editor):
    # Intentionally empty baseline migration: run `manage.py makemigrations itembank`
    # in your development environment to generate a full model-backed migration
    # that matches the current models if you prefer an explicit create-model
    # migration. This file acts as a clean single baseline for the repository.
    return


def noop_reverse(apps, schema_editor):
    # No-op reverse
    return


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.RunPython(noop_forward, reverse_code=noop_reverse),
    ]
