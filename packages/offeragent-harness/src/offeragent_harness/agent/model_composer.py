"""True-streaming model Composer with strict stream conformance checks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from offeragent_harness.models import (
    ModelContentBlock,
    ModelError,
    ModelEventKind,
    ModelFinishReason,
    ModelMessage,
    ModelOutputMode,
    ModelPurpose,
    ModelRequest,
    ModelRole,
    ModelUsage,
    TraceContext,
    thaw_json,
)
from offeragent_harness.ports import CancellationToken, IdGenerator, ModelGateway

from .composer import CompositionEvent, CompositionRetry
from .context_manager import ContextFragment, ContextManager, ContextProjection, ContextWindow
from .model_planner import ModelProviderFailure, ModelStreamProtocolError, validate_usage_progression
from .state import RunState


class ModelComposerError(RuntimeError):
    pass


class CompositionIncomplete(ModelComposerError):
    def __init__(
        self,
        request_id: str,
        finish_reason: ModelFinishReason,
        partial_text: str,
        usage: ModelUsage,
    ) -> None:
        super().__init__(f"composition ended with {finish_reason.value}")
        self.request_id = request_id
        self.finish_reason = finish_reason
        self.partial_text = partial_text
        self.usage = usage


@dataclass(frozen=True, slots=True)
class ComposerModelConfig:
    model: str
    max_output_tokens: int
    reasoning_effort: str | None = None
    temperature: float | None = 0
    seed: int | None = None

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("composer model must not be empty")
        if self.max_output_tokens < 1:
            raise ValueError("composer max_output_tokens must be positive")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("composer temperature must be between 0 and 2")


class ModelComposer:
    def __init__(
        self,
        *,
        gateway: ModelGateway,
        context_manager: ContextManager,
        config: ComposerModelConfig,
        ids: IdGenerator,
    ) -> None:
        self._gateway = gateway
        self._context_manager = context_manager
        self._config = config
        self._ids = ids
        self._hook_context_hints: tuple[str, ...] = ()

    def set_hook_context_hints(self, hints: Sequence[str]) -> None:
        values = tuple(dict.fromkeys(hints))
        if len(values) > 16 or any(not value or len(value) > 4096 for value in values):
            raise ValueError("Hook context hints exceed their count or size limit")
        self._hook_context_hints = values

    def add_memory_context(self, fragments: Sequence[ContextFragment]) -> None:
        self._context_manager = self._context_manager.with_memories(fragments)

    def add_conversation_context(self, fragments: Sequence[ContextFragment]) -> None:
        self._context_manager = self._context_manager.with_conversation(fragments)

    def add_skill_context(self, fragments: Sequence[ContextFragment]) -> None:
        self._context_manager = self._context_manager.with_skills(fragments)

    def create_request(
        self,
        state: RunState,
        *,
        partial: bool,
        projection: ContextProjection | None = None,
        retry_of_request_id: str | None = None,
    ) -> ModelRequest:
        window = self._build_window(state, projection=projection)
        window.ensure_model_ready()
        messages = window.messages
        if partial:
            instruction = ModelMessage(
                ModelRole.SYSTEM,
                (
                    ModelContentBlock.text(
                        "当前 Run 已达到预算或遇到可恢复失败。只基于现有已验证证据说明部分完成状态; "
                        "不得把未执行、未审批或未知结果的写入描述为已完成。"
                    ),
                ),
                name="offeragent-partial-composition",
            )
            system_count = next(
                (index for index, message in enumerate(messages) if message.role is not ModelRole.SYSTEM),
                len(messages),
            )
            messages = (*messages[:system_count], instruction, *messages[system_count:])
        return ModelRequest(
            request_id=self._ids.new_id("model-request"),
            model=self._config.model,
            purpose=ModelPurpose.COMPOSING,
            messages=messages,
            output_mode=ModelOutputMode.TEXT,
            output_schema=None,
            max_output_tokens=self._config.max_output_tokens,
            reasoning_effort=self._config.reasoning_effort,
            temperature=self._config.temperature,
            seed=self._config.seed,
            trace_context=TraceContext(self._ids.new_id("trace")),
            metadata={
                "workspaceId": state.workspace_id,
                "sessionId": state.session_id,
                "turnId": state.turn_id,
                "runId": state.run_id,
                "partial": partial,
                "projection": window.projection.value,
                "projectionHash": window.projection_hash,
                "contextOverflowRetry": retry_of_request_id is not None,
                **({"retryOfRequestId": retry_of_request_id} if retry_of_request_id is not None else {}),
                "omittedContextIds": [item.context_id for item in window.omitted],
            },
        )

    def _build_window(self, state: RunState, *, projection: ContextProjection | None) -> ContextWindow:
        manager = self._context_manager.with_hook_hints(self._hook_context_hints)
        if projection is not None:
            return manager.build(state, purpose=ModelPurpose.COMPOSING, projection=projection)
        normal = manager.build(state, purpose=ModelPurpose.COMPOSING, projection=ContextProjection.NORMAL)
        if not normal.compaction_required:
            return normal
        return manager.build(
            state,
            purpose=ModelPurpose.COMPOSING,
            projection=ContextProjection.OVERFLOW_REFERENCES,
        )

    async def stream(
        self,
        state: RunState,
        *,
        partial: bool,
        cancellation: CancellationToken,
    ) -> AsyncIterator[CompositionEvent]:
        request = self.create_request(state, partial=partial)
        emitted_text = False
        try:
            async for emitted in self._stream_request(request, cancellation):
                emitted_text = emitted_text or emitted.text_delta is not None
                yield emitted
        except ModelProviderFailure as overflow:
            metadata = thaw_json(request.metadata)
            if (
                overflow.error.code != "context_overflow"
                or emitted_text
                or metadata.get("projection") != ContextProjection.NORMAL.value
            ):
                raise
            retry = self.create_request(
                state,
                partial=partial,
                projection=ContextProjection.OVERFLOW_REFERENCES,
                retry_of_request_id=request.request_id,
            )
            retry_metadata = thaw_json(retry.metadata)
            raw_omitted = retry_metadata.get("omittedContextIds", [])
            yield CompositionEvent(
                retry=CompositionRetry(
                    request_id=retry.request_id,
                    retry_of_request_id=request.request_id,
                    projection=ContextProjection.OVERFLOW_REFERENCES.value,
                    projection_hash=(
                        retry_metadata["projectionHash"]
                        if isinstance(retry_metadata.get("projectionHash"), str)
                        else None
                    ),
                    omitted_context_ids=(
                        tuple(item for item in raw_omitted if isinstance(item, str))
                        if isinstance(raw_omitted, list)
                        else ()
                    ),
                )
            )
            async for emitted in self._stream_request(retry, cancellation):
                yield emitted

    async def _stream_request(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[CompositionEvent]:
        expected_sequence = 1
        started = False
        completed = False
        finish_reason: ModelFinishReason | None = None
        usage: ModelUsage | None = None
        text_parts: list[str] = []

        async for event in self._gateway.stream(request, cancellation):
            cancellation.checkpoint()
            if completed:
                raise ModelStreamProtocolError(request.request_id, "received an event after completed", usage=usage)
            if event.request_id != request.request_id:
                raise ModelStreamProtocolError(
                    request.request_id,
                    f"event request_id mismatch: {event.request_id}",
                    usage=usage,
                )
            if event.sequence != expected_sequence:
                raise ModelStreamProtocolError(
                    request.request_id,
                    f"expected sequence {expected_sequence}, received {event.sequence}",
                    usage=usage,
                )
            expected_sequence += 1
            if not started and event.kind is not ModelEventKind.STARTED:
                raise ModelStreamProtocolError(request.request_id, "first event must be started", usage=usage)

            if event.kind is ModelEventKind.STARTED:
                if started:
                    raise ModelStreamProtocolError(request.request_id, "duplicate started event", usage=usage)
                started = True
            elif event.kind is ModelEventKind.TEXT_DELTA:
                if not event.text:
                    raise ModelStreamProtocolError(request.request_id, "empty text delta", usage=usage)
                text_parts.append(event.text)
                # Yield before requesting the next provider event: this is real
                # streaming rather than post-hoc chunking of an aggregated answer.
                yield CompositionEvent(text_delta=event.text)
            elif event.kind is ModelEventKind.REASONING_SUMMARY:
                if not event.text:
                    raise ModelStreamProtocolError(request.request_id, "empty reasoning summary", usage=usage)
                yield CompositionEvent(reasoning_summary_delta=event.text)
            elif event.kind is ModelEventKind.USAGE:
                assert event.usage is not None
                validate_usage_progression(usage, event.usage, request.request_id)
                usage = event.usage
            elif event.kind is ModelEventKind.COMPLETED:
                assert event.finish_reason is not None
                if event.usage is not None:
                    validate_usage_progression(usage, event.usage, request.request_id)
                    usage = event.usage
                finish_reason = event.finish_reason
                completed = True
            elif event.kind is ModelEventKind.ERROR:
                assert event.error is not None
                if usage is not None:
                    yield CompositionEvent(usage=usage)
                raise ModelProviderFailure(request.request_id, event.error, usage=usage)
            elif event.kind is ModelEventKind.CANCELLED:
                if usage is not None:
                    yield CompositionEvent(usage=usage)
                raise ModelProviderFailure(
                    request.request_id,
                    ModelError("provider_cancelled", "model provider cancelled the request", False, True),
                    usage=usage,
                )
            elif event.kind is ModelEventKind.STRUCTURED_OUTPUT:
                raise ModelStreamProtocolError(
                    request.request_id,
                    "structured output is invalid in text composition mode",
                    usage=usage,
                )
            cancellation.checkpoint()

        if not started:
            raise ModelStreamProtocolError(request.request_id, "stream ended before started", usage=usage)
        if not completed or finish_reason is None:
            raise ModelStreamProtocolError(request.request_id, "stream ended before completed", usage=usage)
        if usage is None:
            raise ModelStreamProtocolError(request.request_id, "completed stream omitted usage")
        if not text_parts:
            raise ModelStreamProtocolError(request.request_id, "completed composition contained no text", usage=usage)

        # Emit exactly one final usage snapshot so the outer Agent Loop cannot
        # double-charge intermediate cumulative provider snapshots.
        yield CompositionEvent(usage=usage)
        if finish_reason is not ModelFinishReason.STOP:
            raise CompositionIncomplete(request.request_id, finish_reason, "".join(text_parts), usage)


__all__ = [
    "ComposerModelConfig",
    "CompositionIncomplete",
    "ModelComposer",
    "ModelComposerError",
]
