from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("database", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "ALTER TABLE database_entry DROP COLUMN IF EXISTS embeddings;",
                "ALTER TABLE database_entry DROP COLUMN IF EXISTS search_model_id;",
                "ALTER TABLE database_usermemory DROP COLUMN IF EXISTS embeddings;",
                "ALTER TABLE database_usermemory DROP COLUMN IF EXISTS search_model_id;",
                "DROP TABLE IF EXISTS database_entrydates CASCADE;",
                "DROP TABLE IF EXISTS database_searchmodelconfig CASCADE;",
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
