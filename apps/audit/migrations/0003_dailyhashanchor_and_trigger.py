"""DailyHashAnchor model + DB trigger preventing UPDATE/DELETE on audit_auditevent.

The trigger is the structural enforcement of "append-only". Application code
can hit the table via the normal ORM path; abusive code (or a compromised
admin) cannot mutate or remove rows without the explicit session bypass.

Tests run inside a transaction and need to clean up. The trigger respects
the ``app.audit_admin`` GUC — when a connection sets it to ``'true'`` the
trigger allows mutations. Production never sets this; the test fixture
``audit_admin_bypass`` does.

Postgres-only. SQLite (used by some local dev) gets a no-op trigger because
SQLite doesn't support row-level triggers the same way; behaviour is
guaranteed at the model layer instead.
"""
import django.utils.timezone
from django.db import migrations, models


# Note on the doubled %% below. Django's schema_editor.execute() hands the
# SQL through psycopg's mogrify(), which always treats a bare `%` as a
# parameter placeholder — even when no params are supplied. We need a
# literal `%` to survive into the PL/pgSQL `RAISE EXCEPTION` format string,
# so we escape it as `%%`. After mogrify it becomes the single `%` the
# function body expects.
CREATE_TRIGGER_SQL = r"""
CREATE OR REPLACE FUNCTION audit_auditevent_block_mutations()
RETURNS trigger AS $$
BEGIN
    IF coalesce(current_setting('app.audit_admin', true), 'false') = 'true' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'audit_auditevent is append-only; %% blocked by trigger', TG_OP
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_auditevent_no_update ON audit_auditevent;
CREATE TRIGGER audit_auditevent_no_update
BEFORE UPDATE ON audit_auditevent
FOR EACH ROW EXECUTE FUNCTION audit_auditevent_block_mutations();

DROP TRIGGER IF EXISTS audit_auditevent_no_delete ON audit_auditevent;
CREATE TRIGGER audit_auditevent_no_delete
BEFORE DELETE ON audit_auditevent
FOR EACH ROW EXECUTE FUNCTION audit_auditevent_block_mutations();
"""


DROP_TRIGGER_SQL = r"""
DROP TRIGGER IF EXISTS audit_auditevent_no_update ON audit_auditevent;
DROP TRIGGER IF EXISTS audit_auditevent_no_delete ON audit_auditevent;
DROP FUNCTION IF EXISTS audit_auditevent_block_mutations();
"""


def _run_trigger_sql_on_postgres(apps, schema_editor):
    """Only execute on Postgres. SQLite ignores this step."""
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(CREATE_TRIGGER_SQL)


def _drop_trigger_sql_on_postgres(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(DROP_TRIGGER_SQL)


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0002_alter_auditevent_entity_type"),
    ]

    operations = [
        migrations.CreateModel(
            name="DailyHashAnchor",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("date", models.DateField(db_index=True, unique=True)),
                ("head_event_id", models.UUIDField(blank=True, null=True)),
                ("head_hash", models.CharField(max_length=64)),
                ("event_count", models.PositiveIntegerField(default=0)),
                ("exported_to_s22_at", models.DateTimeField(blank=True, null=True)),
                ("anchor_ref", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "verbose_name": "Daily Hash Anchor",
                "db_table": "audit_dailyhashanchor",
                "ordering": ["-date"],
            },
        ),
        migrations.RunPython(
            _run_trigger_sql_on_postgres,
            reverse_code=_drop_trigger_sql_on_postgres,
        ),
    ]
