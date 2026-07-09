from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("database", "0002_drop_legacy_vector_search_artifacts"),
    ]

    operations = [
        migrations.RunSQL(
            sql="DROP TABLE IF EXISTS database_usermemory CASCADE;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
