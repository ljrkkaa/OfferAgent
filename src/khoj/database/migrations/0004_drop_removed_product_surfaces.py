from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("database", "0003_drop_legacy_usermemory"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "DROP TABLE IF EXISTS database_publicconversation CASCADE;",
                "DROP TABLE IF EXISTS database_subscription CASCADE;",
                "DROP TABLE IF EXISTS database_uservoicemodelconfig CASCADE;",
                "DROP TABLE IF EXISTS database_voicemodeloption CASCADE;",
                "DROP TABLE IF EXISTS database_speechtotextmodeloptions CASCADE;",
                "DROP TABLE IF EXISTS database_googleuser CASCADE;",
                "DROP TABLE IF EXISTS database_githubrepoconfig CASCADE;",
                "DROP TABLE IF EXISTS database_githubconfig CASCADE;",
                "DROP TABLE IF EXISTS database_notionconfig CASCADE;",
                "DROP TABLE IF EXISTS database_usertexttoimagemodelconfig CASCADE;",
                "DROP TABLE IF EXISTS database_texttoimagemodelconfig CASCADE;",
                "ALTER TABLE database_chatmodel DROP COLUMN IF EXISTS price_tier;",
                "ALTER TABLE database_chatmodel DROP COLUMN IF EXISTS subscribed_max_prompt_size;",
                "ALTER TABLE database_khojuser DROP COLUMN IF EXISTS email_verification_code;",
                "ALTER TABLE database_khojuser DROP COLUMN IF EXISTS email_verification_code_expiry;",
                "ALTER TABLE database_serverchatsettings DROP COLUMN IF EXISTS chat_advanced_id;",
                "ALTER TABLE database_serverchatsettings DROP COLUMN IF EXISTS think_free_fast_id;",
                "ALTER TABLE database_serverchatsettings DROP COLUMN IF EXISTS think_free_deep_id;",
                "ALTER TABLE database_serverchatsettings DROP COLUMN IF EXISTS think_paid_fast_id;",
                "ALTER TABLE database_serverchatsettings DROP COLUMN IF EXISTS think_paid_deep_id;",
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
