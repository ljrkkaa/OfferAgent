import csv
import json
from datetime import datetime, timedelta

from apscheduler.job import Job
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.http import HttpResponse
from django_apscheduler.admin import DjangoJobAdmin, DjangoJobExecutionAdmin
from django_apscheduler.jobstores import DjangoJobStore
from django_apscheduler.models import DjangoJob, DjangoJobExecution
from unfold import admin as unfold_admin

from khoj.database.models import (
    AiModelApi,
    ChatModel,
    Conversation,
    Entry,
    KhojUser,
    McpServer,
    ProcessLock,
    RateLimitRecord,
    ServerChatSettings,
    UserConversationConfig,
    UserRequests,
    WebScraper,
)


class KhojDjangoJobAdmin(DjangoJobAdmin, unfold_admin.ModelAdmin):
    list_display = (
        "id",
        "next_run_time",
        "job_info",
    )
    search_fields = ("id", "next_run_time")
    ordering = ("-next_run_time",)
    job_store = DjangoJobStore()

    def job_info(self, obj):
        job: Job = self.job_store.lookup_job(obj.id)
        return f"{job.func_ref} {job.args} {job.kwargs}" if job else "None"

    job_info.short_description = "Job Info"  # type: ignore

    def get_search_results(self, request, queryset, search_term):
        queryset, use_distinct = super().get_search_results(request, queryset, search_term)
        if search_term:
            jobs = [job.id for job in self.job_store.get_all_jobs() if search_term in str(job)]
            queryset |= self.model.objects.filter(id__in=jobs)
        return queryset, use_distinct


class KhojDjangoJobExecutionAdmin(DjangoJobExecutionAdmin, unfold_admin.ModelAdmin):
    pass


admin.site.unregister(DjangoJob)
admin.site.register(DjangoJob, KhojDjangoJobAdmin)
admin.site.unregister(DjangoJobExecution)
admin.site.register(DjangoJobExecution, KhojDjangoJobExecutionAdmin)


class GroupAdmin(BaseGroupAdmin, unfold_admin.ModelAdmin):
    pass


class UserAdmin(BaseUserAdmin, unfold_admin.ModelAdmin):
    pass


class KhojUserAdmin(UserAdmin, unfold_admin.ModelAdmin):
    class DateJoinedAfterFilter(admin.SimpleListFilter):
        title = "Joined after"
        parameter_name = "joined_after"

        def lookups(self, request, model_admin):
            return (
                ("1d", "Last 24 hours"),
                ("7d", "Last 7 days"),
                ("30d", "Last 30 days"),
                ("90d", "Last 90 days"),
            )

        def queryset(self, request, queryset):
            if self.value():
                days = int(self.value().rstrip("d"))
                date_threshold = datetime.now() - timedelta(days=days)
                return queryset.filter(date_joined__gte=date_threshold)
            return queryset

    list_display = (
        "id",
        "email",
        "username",
        "is_active",
        "uuid",
        "is_staff",
        "is_superuser",
    )
    search_fields = ("email", "username", "uuid")
    filter_horizontal = ("groups", "user_permissions")

    list_filter = (
        DateJoinedAfterFilter,
        "verified_email",
    ) + UserAdmin.list_filter

    fieldsets = (
        (
            "Personal info",
            {"fields": ("verified_email",)},
        ),
    ) + UserAdmin.fieldsets


admin.site.unregister(Group)
admin.site.register(KhojUser, KhojUserAdmin)

admin.site.register(ProcessLock, unfold_admin.ModelAdmin)
admin.site.register(UserRequests, unfold_admin.ModelAdmin)
admin.site.register(RateLimitRecord, unfold_admin.ModelAdmin)


@admin.register(McpServer)
class McpServerAdmin(unfold_admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "path",
    )
    search_fields = ("id", "name", "path")


@admin.register(Entry)
class EntryAdmin(unfold_admin.ModelAdmin):
    list_display = (
        "id",
        "created_at",
        "updated_at",
        "user",
        "file_source",
        "file_type",
        "file_name",
        "file_path",
    )
    search_fields = ("id", "user__email", "user__username", "file_path")
    list_filter = (
        "file_type",
        "user__email",
    )
    ordering = ("-created_at",)


@admin.register(ChatModel)
class ChatModelAdmin(unfold_admin.ModelAdmin):
    list_display = (
        "id",
        "friendly_name",
        "name",
        "ai_model_api",
        "max_prompt_size",
    )
    search_fields = ("id", "name", "ai_model_api__name")


@admin.register(AiModelApi)
class AiModelApiAdmin(unfold_admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "api_base_url",
        "api_key",
    )
    search_fields = ("id", "name", "api_base_url", "api_key")


@admin.register(ServerChatSettings)
class ServerChatSettingsAdmin(unfold_admin.ModelAdmin):
    list_display = (
        "priority",
        "chat_default",
        "web_scraper",
        "memory_mode",
    )
    ordering = ("priority",)


@admin.register(WebScraper)
class WebScraperAdmin(unfold_admin.ModelAdmin):
    list_display = (
        "priority",
        "name",
        "type",
        "api_key",
        "api_url",
        "created_at",
    )
    search_fields = ("name", "api_key", "api_url", "type")
    ordering = ("priority",)


@admin.register(Conversation)
class ConversationAdmin(unfold_admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "created_at",
        "updated_at",
    )
    search_fields = ("id", "user__email", "user__username")
    list_filter = ("user",)
    ordering = ("-created_at",)

    actions = ["export_selected_objects", "export_selected_minimal_objects"]

    def export_selected_objects(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="conversations.csv"'

        writer = csv.writer(response)
        writer.writerow(["id", "user", "created_at", "updated_at", "conversation_log"])

        for conversation in queryset:
            modified_log = conversation.conversation_log
            chat_log = modified_log.get("chat", [])
            for idx, log in enumerate(chat_log):
                if log["by"] == "khoj" and log["images"]:
                    log["images"] = ["inline image redacted for space"]
                    chat_log[idx] = log

            modified_log["chat"] = chat_log

            writer.writerow(
                [
                    conversation.id,
                    conversation.user,
                    conversation.created_at,
                    conversation.updated_at,
                    json.dumps(modified_log),
                ]
            )

        return response

    export_selected_objects.short_description = "Export selected conversations"  # type: ignore

    def export_selected_minimal_objects(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="conversations.csv"'

        writer = csv.writer(response)
        writer.writerow(["id", "user", "created_at", "updated_at", "conversation_log"])

        fields_to_keep = set(["message", "by", "created"])

        for conversation in queryset:
            return_log = dict()
            chat_log = conversation.conversation_log.get("chat", [])
            for idx, log in enumerate(chat_log):
                updated_log = {}
                for key in fields_to_keep:
                    updated_log[key] = log[key]
                chat_log[idx] = updated_log
            return_log["chat"] = chat_log

            writer.writerow(
                [
                    conversation.id,
                    conversation.user,
                    conversation.created_at,
                    conversation.updated_at,
                    json.dumps(return_log),
                ]
            )

        return response

    export_selected_minimal_objects.short_description = "Export selected conversations (minimal)"  # type: ignore

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            if "export_selected_objects" in actions:
                del actions["export_selected_objects"]
            if "export_selected_minimal_objects" in actions:
                del actions["export_selected_minimal_objects"]
        return actions


@admin.register(UserConversationConfig)
class UserConversationConfigAdmin(unfold_admin.ModelAdmin):
    list_display = (
        "id",
        "get_user_email",
        "get_chat_model",
    )
    search_fields = ("id", "user__email", "setting__name")
    ordering = ("-updated_at",)

    def get_user_email(self, obj):
        return obj.user.email

    get_user_email.short_description = "User Email"  # type: ignore
    get_user_email.admin_order_field = "user__email"  # type: ignore

    def get_chat_model(self, obj):
        return obj.setting.name if obj.setting else None

    get_chat_model.short_description = "Chat Model"  # type: ignore
    get_chat_model.admin_order_field = "setting__name"  # type: ignore
