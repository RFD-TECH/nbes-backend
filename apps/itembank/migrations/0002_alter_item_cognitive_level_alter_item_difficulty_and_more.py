"""No-op compatibility migration.

This migration was generated against a local model state that did not match
0001_initial.py, so it tried to alter fields that do not exist in the
migration graph. Keep the dependency slot so later migrations remain ordered,
but do not execute invalid schema operations on fresh databases.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('itembank', '0001_initial'),
    ]

    operations = []
