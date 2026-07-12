from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("database", "0008_hard_delete_unused_surfaces"),
    ]

    operations = [
        migrations.RunSQL(
            sql="DROP TABLE IF EXISTS database_mcpserver CASCADE;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
