import logging
import os
import uuid
from typing import Dict, List, Optional, Union

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from pydantic import BaseModel as PydanticBaseModel
from pydantic import Field, model_validator

logger = logging.getLogger(__name__)


# Pydantic models for type Chat Message validation
class Context(PydanticBaseModel):
    compiled: str
    file: str
    uri: Optional[str] = None
    query: Optional[str] = None

    @model_validator(mode="after")
    def set_uri_fallback(self):
        """Set the URI to existing deeplink URI. Fallback to file based URI if unset."""
        if self.uri and self.uri.strip():
            self.uri = self.uri
        elif self.file and (self.file.startswith("http") or self.file.startswith("file://")):
            self.uri = self.file
        elif self.file:
            self.uri = f"file://{self.file}"
        else:
            self.uri = None
        return self


class WebPage(PydanticBaseModel):
    link: str
    query: Optional[str] = None
    snippet: str


class AnswerBox(PydanticBaseModel):
    link: Optional[str] = None
    snippet: Optional[str] = None
    title: str
    snippetHighlighted: Optional[List[str]] = None


class PeopleAlsoAsk(PydanticBaseModel):
    link: Optional[str] = None
    question: Optional[str] = None
    snippet: Optional[str] = None
    title: Optional[str] = None


class KnowledgeGraph(PydanticBaseModel):
    attributes: Optional[Dict[str, str]] = None
    description: Optional[str] = None
    descriptionLink: Optional[str] = None
    descriptionSource: Optional[str] = None
    imageUrl: Optional[str] = None
    title: str
    type: Optional[str] = None


class OrganicContext(PydanticBaseModel):
    snippet: Optional[str] = None
    title: str
    link: str


class OnlineContext(PydanticBaseModel):
    webpages: Optional[Union[WebPage, List[WebPage]]] = None
    answerBox: Optional[AnswerBox] = None
    peopleAlsoAsk: Optional[List[PeopleAlsoAsk]] = None
    knowledgeGraph: Optional[KnowledgeGraph] = None
    organic: Optional[List[OrganicContext]] = None


class Intent(PydanticBaseModel):
    type: str
    query: Optional[str] = None
    memory_type: Optional[str] = Field(alias="memory-type", default=None)
    inferred_queries: Optional[List[str]] = Field(default=None, alias="inferred-queries")


class TrainOfThought(PydanticBaseModel):
    type: str
    data: str


class ChatMessageModel(PydanticBaseModel):
    by: str
    message: str | list[dict]
    trainOfThought: List[TrainOfThought] = []
    context: List[Context] = []
    onlineContext: Dict[str, OnlineContext] = {}
    created: Optional[str] = None
    images: Optional[List[str]] = None
    queryFiles: Optional[List[Dict]] = None
    artifacts: Optional[List[Dict]] = None
    turnId: Optional[str] = None
    intent: Optional[Intent] = None
    automationId: Optional[str] = None


class DbBaseModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class KhojUser(AbstractUser):
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    verified_email = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        if not self.uuid:
            self.uuid = uuid.uuid4()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.username} ({self.uuid})"


class KhojApiUser(models.Model):
    """User issued API tokens to authenticate Khoj clients"""

    user = models.ForeignKey(KhojUser, on_delete=models.CASCADE)
    token = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=50)
    accessed_at = models.DateTimeField(null=True, default=None)


class AiModelApi(DbBaseModel):
    name = models.CharField(max_length=200)
    api_key = models.CharField(max_length=4000)
    api_base_url = models.URLField(max_length=200, default=None, blank=True, null=True)

    def __str__(self):
        return self.name


class ChatModel(DbBaseModel):
    class ModelType(models.TextChoices):
        OPENAI = "openai"
        ANTHROPIC = "anthropic"
        GOOGLE = "google"

    max_prompt_size = models.IntegerField(default=None, null=True, blank=True)
    name = models.CharField(max_length=200, default="gemini-2.5-flash")
    friendly_name = models.CharField(max_length=200, default=None, null=True, blank=True)
    model_type = models.CharField(max_length=200, choices=ModelType.choices, default=ModelType.GOOGLE)
    vision_enabled = models.BooleanField(default=False)
    ai_model_api = models.ForeignKey(AiModelApi, on_delete=models.CASCADE, default=None, null=True, blank=True)
    description = models.TextField(default=None, null=True, blank=True)
    strengths = models.TextField(default=None, null=True, blank=True)

    def __str__(self):
        return self.friendly_name or self.name


class Agent(DbBaseModel):
    name = models.CharField(max_length=200)
    personality = models.TextField(default=None, null=True, blank=True)
    chat_model = models.ForeignKey(ChatModel, on_delete=models.CASCADE)
    slug = models.CharField(max_length=200, unique=True, default="khoj")

    def __str__(self):
        return self.name


class ProcessLock(DbBaseModel):
    class Operation(models.TextChoices):
        INDEX_CONTENT = "index_content"
        SCHEDULED_JOB = "scheduled_job"
        SCHEDULE_LEADER = "schedule_leader"
        APPLY_MIGRATIONS = "apply_migrations"

    # We need to make sure that some operations are thread-safe. To do so, add locks for potentially shared operations.
    # For example, only one process should index content at a time.
    name = models.CharField(max_length=200, choices=Operation.choices, unique=True)
    started_at = models.DateTimeField(auto_now_add=True)
    max_duration_in_seconds = models.IntegerField(default=60 * 60 * 12)  # 12 hours


class WebScraper(DbBaseModel):
    class WebScraperType(models.TextChoices):
        FIRECRAWL = "Firecrawl"
        OLOSTEP = "Olostep"
        EXA = "Exa"
        DIRECT = "Direct"

    name = models.CharField(
        max_length=200,
        default=None,
        null=True,
        blank=True,
        unique=True,
        help_text="Friendly name. If not set, it will be set to the type of the scraper.",
    )
    type = models.CharField(max_length=20, choices=WebScraperType.choices, default=WebScraperType.DIRECT)
    api_key = models.CharField(
        max_length=200,
        default=None,
        null=True,
        blank=True,
        help_text="API key of the web scraper. Only set if scraper service requires an API key. Default is set from env var.",
    )
    api_url = models.URLField(
        max_length=200,
        default=None,
        null=True,
        blank=True,
        help_text="API URL of the web scraper. Only set if scraper service on non-default URL.",
    )
    priority = models.IntegerField(
        default=None,
        null=True,
        blank=True,
        unique=True,
        help_text="Priority of the web scraper. Lower numbers run first.",
    )

    def clean(self):
        error = {}
        if self.name is None:
            self.name = self.type.capitalize()
        if self.api_url is None:
            if self.type == self.WebScraperType.FIRECRAWL:
                self.api_url = os.getenv("FIRECRAWL_API_URL", "https://api.firecrawl.dev")
            elif self.type == self.WebScraperType.OLOSTEP:
                self.api_url = os.getenv("OLOSTEP_API_URL", "https://agent.olostep.com/olostep-p2p-incomingAPI")
            elif self.type == self.WebScraperType.EXA:
                self.api_url = os.getenv("EXA_API_URL", "https://api.exa.ai")
        if self.api_key is None:
            if self.type == self.WebScraperType.FIRECRAWL:
                self.api_key = os.getenv("FIRECRAWL_API_KEY")
                if not self.api_key and self.api_url == "https://api.firecrawl.dev":
                    error["api_key"] = "Set API key to use default Firecrawl. Get API key from https://firecrawl.dev."
            elif self.type == self.WebScraperType.OLOSTEP:
                self.api_key = os.getenv("OLOSTEP_API_KEY")
                if self.api_key is None:
                    error["api_key"] = "Set API key to use Olostep. Get API key from https://olostep.com/."
            elif self.type == self.WebScraperType.EXA:
                self.api_key = os.getenv("EXA_API_KEY")
                if self.api_key is None:
                    error["api_key"] = "Set API key to use Exa. Get API key from https://exa.ai/."
        if error:
            raise ValidationError(error)

    def save(self, *args, **kwargs):
        self.clean()

        if self.priority is None:
            max_priority = WebScraper.objects.aggregate(models.Max("priority"))["priority__max"]
            self.priority = max_priority + 1 if max_priority else 1

        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ServerChatSettings(DbBaseModel):
    class ChatModelSlot(models.TextChoices):
        """Enum for the different chat model slots in ServerChatSettings"""

        CHAT_DEFAULT = "chat_default"

    class MemoryMode(models.TextChoices):
        """Enum for server-level memory feature configuration"""

        DISABLED = "disabled", "Disabled"
        ENABLED_DEFAULT_OFF = "enabled_default_off", "Enabled, default off"
        ENABLED_DEFAULT_ON = "enabled_default_on", "Enabled, default on"

    chat_default = models.ForeignKey(
        ChatModel, on_delete=models.CASCADE, default=None, null=True, blank=True, related_name="chat_default"
    )
    web_scraper = models.ForeignKey(
        WebScraper, on_delete=models.CASCADE, default=None, null=True, blank=True, related_name="web_scraper"
    )
    priority = models.IntegerField(
        default=None,
        null=True,
        blank=True,
        unique=True,
        help_text="Priority of the server chat settings. Lower numbers run first.",
    )
    memory_mode = models.CharField(
        max_length=20,
        choices=MemoryMode.choices,
        default=MemoryMode.ENABLED_DEFAULT_ON,
        help_text="Server-level memory feature configuration. Disabled overrides user preference.",
    )

    def save(self, *args, **kwargs):
        if self.priority is None:
            max_priority = ServerChatSettings.objects.aggregate(models.Max("priority"))["priority__max"]
            self.priority = max_priority + 1 if max_priority else 1

        super().save(*args, **kwargs)


class UserConversationConfig(DbBaseModel):
    user = models.OneToOneField(KhojUser, on_delete=models.CASCADE)
    setting = models.ForeignKey(ChatModel, on_delete=models.CASCADE, default=None, null=True, blank=True)
    enable_memory = models.BooleanField(default=True)


class Conversation(DbBaseModel):
    user = models.ForeignKey(KhojUser, on_delete=models.CASCADE)
    conversation_log = models.JSONField(default=dict)

    # Slug is an app-generated conversation identifier. Need not be unique. Used as display title essentially.
    slug = models.CharField(max_length=200, default=None, null=True, blank=True)

    # The title field is explicitly set by the user.
    title = models.CharField(max_length=500, default=None, null=True, blank=True)
    agent = models.ForeignKey(Agent, on_delete=models.SET_NULL, default=None, null=True, blank=True)
    file_filters = models.JSONField(default=list)
    id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, primary_key=True, db_index=True)

    def normalize_conversation_log(self):
        if isinstance(self.conversation_log, list):
            self.conversation_log = {"chat": self.conversation_log}
        elif self.conversation_log is None:
            self.conversation_log = {"chat": []}
        if isinstance(self.conversation_log, dict):
            self.conversation_log["chat"] = [
                msg.model_dump(mode="json") if hasattr(msg, "model_dump") else msg
                for msg in self.conversation_log.get("chat", [])
            ]

    def clean(self):
        self.normalize_conversation_log()
        # Validate conversation_log structure
        try:
            messages = self.conversation_log.get("chat", [])
            for msg in messages:
                ChatMessageModel.model_validate(msg)
        except Exception as e:
            raise ValidationError(f"Invalid conversation_log format: {str(e)}")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    @property
    def messages(self) -> List[ChatMessageModel]:
        """Type-hinted accessor for conversation messages"""
        validated_messages = []
        for msg in self.conversation_log.get("chat", []):
            try:
                # Clean up inferred queries if they contain None
                if msg.get("intent") and msg["intent"].get("inferred_queries"):
                    msg["intent"]["inferred-queries"] = [
                        q for q in msg["intent"]["inferred_queries"] if q is not None and isinstance(q, str)
                    ]
                msg["message"] = str(msg.get("message", ""))
                validated_messages.append(ChatMessageModel.model_validate(msg))
            except ValidationError as e:
                logger.warning(f"Skipping invalid message in conversation: {e}")
                continue
        return validated_messages


class VaultActionBatch(DbBaseModel):
    class Status(models.TextChoices):
        PENDING = "pending"
        APPLYING = "applying"
        APPLIED = "applied"
        CANCELLED = "cancelled"
        CONFLICT = "conflict"
        FAILED = "failed"
        EXPIRED = "expired"
        MANUAL_REVIEW_REQUIRED = "manual_review_required"

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    user = models.ForeignKey(KhojUser, on_delete=models.CASCADE)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE)
    turn_id = models.UUIDField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING)
    actions = models.JSONField(default=list)
    snapshots = models.JSONField(default=dict)
    previews = models.JSONField(default=list)
    root_fingerprint = models.CharField(max_length=64)
    action_digest = models.CharField(max_length=64)
    rollback_journal = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "turn_id"],
                name="unique_vault_batch_turn",
            )
        ]
        indexes = [
            models.Index(
                fields=["user", "conversation", "status", "created_at"],
                name="vault_batch_owner_status_idx",
            ),
            models.Index(fields=["status", "expires_at"], name="vault_batch_expiry_idx"),
        ]


class FileObject(DbBaseModel):
    # Contains the full text of a file that has associated Entry objects
    file_name = models.CharField(max_length=400, default=None, null=True, blank=True)
    raw_text = models.TextField()
    user = models.ForeignKey(KhojUser, on_delete=models.CASCADE, default=None, null=True, blank=True)


class Entry(DbBaseModel):
    class EntryType(models.TextChoices):
        PDF = "pdf"
        PLAINTEXT = "plaintext"
        MARKDOWN = "markdown"
        CONVERSATION = "conversation"

    class EntrySource(models.TextChoices):
        COMPUTER = "computer"

    user = models.ForeignKey(KhojUser, on_delete=models.CASCADE, default=None, null=True, blank=True)
    raw = models.TextField()
    compiled = models.TextField()
    heading = models.CharField(max_length=1000, default=None, null=True, blank=True)
    file_source = models.CharField(max_length=30, choices=EntrySource.choices, default=EntrySource.COMPUTER)
    file_type = models.CharField(max_length=30, choices=EntryType.choices, default=EntryType.PLAINTEXT)
    file_path = models.CharField(max_length=400, default=None, null=True, blank=True)
    file_name = models.CharField(max_length=400, default=None, null=True, blank=True)
    url = models.URLField(max_length=400, default=None, null=True, blank=True)
    hashed_value = models.CharField(max_length=100)
    corpus_id = models.UUIDField(default=uuid.uuid4, editable=False)
    file_object = models.ForeignKey(FileObject, on_delete=models.CASCADE, default=None, null=True, blank=True)


class UserRequests(DbBaseModel):
    """Stores user requests to the server for rate limiting."""

    user = models.ForeignKey(KhojUser, on_delete=models.CASCADE)
    slug = models.CharField(max_length=200)


class RateLimitRecord(DbBaseModel):
    """Stores individual request timestamps for rate limiting."""

    identifier = models.CharField(max_length=255, db_index=True)  # IP address or email
    slug = models.CharField(max_length=255, db_index=True)  # Differentiates limit types

    class Meta:
        indexes = [
            models.Index(fields=["identifier", "slug", "created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.slug} - {self.identifier} at {self.created_at}"
