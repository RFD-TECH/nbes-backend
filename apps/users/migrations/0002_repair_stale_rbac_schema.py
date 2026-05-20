"""Repair dev databases that applied the older users.0001 schema.

The solomon branch briefly had an IAM-style users schema under the same
``users.0001_initial`` migration name. A developer who migrated during that
window can have ``django_migrations`` saying 0001 is applied while the database
still has legacy columns and lacks the RBAC matrix tables. This migration is a
guarded database repair: it is a no-op on a clean schema, and recreates only the
local NBES user/RBAC tables when the stale shape is detected.
"""
from importlib import import_module

from django.db import migrations


def repair_stale_schema(apps, schema_editor):
    connection = schema_editor.connection
    existing_tables = set(connection.introspection.table_names())

    def columns(table_name):
        if table_name not in existing_tables:
            return set()
        with connection.cursor() as cursor:
            return {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor,
                    table_name,
                )
            }

    profile_columns = columns("users_userprofile")
    stale_profile = (
        "users_userprofile" in existing_tables
        and (
            "keycloak_sub" not in profile_columns
            or "first_name" in profile_columns
            or "password_hash" in profile_columns
            or "invite_token" in profile_columns
        )
    )

    if stale_profile:
        schema_editor.execute("DROP TABLE IF EXISTS users_userprofile CASCADE")
        existing_tables.discard("users_userprofile")

    # If users_role exists but is_custom column is missing (stale volume from
    # before is_custom was added to 0001_initial), add it now so 0003 doesn't fail.
    if "users_role" in existing_tables and "is_custom" not in columns("users_role"):
        schema_editor.execute(
            "ALTER TABLE users_role ADD COLUMN is_custom boolean NOT NULL DEFAULT true"
        )
        schema_editor.execute(
            "CREATE INDEX IF NOT EXISTS users_role_is_custom ON users_role (is_custom)"
        )

    model_order = [
        "UserProfile",
        "Permission",
        "Role",
        "RolePermission",
    ]
    for model_name in model_order:
        model = apps.get_model("users", model_name)
        if model._meta.db_table not in existing_tables:
            schema_editor.create_model(model)
            existing_tables.add(model._meta.db_table)

    seed_matrix = import_module("apps.users.migrations.0001_initial").seed_matrix
    seed_matrix(apps, schema_editor)


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            repair_stale_schema,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
