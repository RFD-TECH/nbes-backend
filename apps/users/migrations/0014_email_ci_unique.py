from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0013_rename_users_roleap_status_exp_idx_users_rolea_status_538550_idx_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            sql="CREATE UNIQUE INDEX users_userprofile_email_ci ON users_userprofile (LOWER(email));",
            reverse_sql="DROP INDEX users_userprofile_email_ci;",
        )
    ]
