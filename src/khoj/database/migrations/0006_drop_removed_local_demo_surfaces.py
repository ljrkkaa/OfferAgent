from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("database", "0005_alter_agent_output_modes"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                'ALTER TABLE "database_khojuser" DROP COLUMN IF EXISTS "phone_number";',
                'ALTER TABLE "database_khojuser" DROP COLUMN IF EXISTS "verified_phone_number";',
                'ALTER TABLE "database_agent" DROP COLUMN IF EXISTS "privacy_level";',
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
