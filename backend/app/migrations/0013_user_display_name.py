"""Give a user a name a person can read, separate from their identity key (#84).

Backfills to ``""`` rather than to ``username``: the existing values *are* the synthesized
``<sub>.<issuer-hash>`` keys, so copying them across would seed the exact string this field exists to
stop showing anyone. Existing rows fill in by themselves on the owner's next authenticated request,
which is when their token is next read.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0012_usagerecord_rls"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="display_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
