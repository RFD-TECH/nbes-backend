from django.conf import settings
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('itembank', '0002_alter_item_cognitive_level_alter_item_difficulty_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = []
