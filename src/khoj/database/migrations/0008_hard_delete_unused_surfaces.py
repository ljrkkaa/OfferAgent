from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("database", "0007_vaultactionbatch"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "UPDATE database_conversation SET agent_id = (SELECT id FROM database_agent WHERE slug = 'khoj' LIMIT 1) WHERE agent_id IS DISTINCT FROM (SELECT id FROM database_agent WHERE slug = 'khoj' LIMIT 1);",
                "ALTER TABLE database_fileobject DROP COLUMN IF EXISTS agent_id CASCADE;",
                "ALTER TABLE database_entry DROP COLUMN IF EXISTS agent_id CASCADE;",
                "DELETE FROM database_agent WHERE slug IS NULL OR slug <> 'khoj';",
                "DELETE FROM database_khojapiuser;",
                "ALTER TABLE database_conversation DROP COLUMN IF EXISTS client_id CASCADE;",
                "ALTER TABLE database_chatmodel DROP COLUMN IF EXISTS tokenizer CASCADE;",
                "ALTER TABLE database_agent DROP COLUMN IF EXISTS creator_id CASCADE;",
                "ALTER TABLE database_agent DROP COLUMN IF EXISTS input_tools CASCADE;",
                "ALTER TABLE database_agent DROP COLUMN IF EXISTS output_modes CASCADE;",
                "ALTER TABLE database_agent DROP COLUMN IF EXISTS managed_by_admin CASCADE;",
                "ALTER TABLE database_agent DROP COLUMN IF EXISTS style_color CASCADE;",
                "ALTER TABLE database_agent DROP COLUMN IF EXISTS style_icon CASCADE;",
                "ALTER TABLE database_agent DROP COLUMN IF EXISTS is_hidden CASCADE;",
                "DROP TABLE IF EXISTS database_clientapplication CASCADE;",
                "DROP TABLE IF EXISTS database_datastore CASCADE;",
                "DROP TABLE IF EXISTS database_reflectivequestion CASCADE;",
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
