from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar

import pytest
from pydantic import SecretStr

from offeragent_harness.ports import (
    ApplicationCommandContext,
    SecretHandle,
    SecretInput,
    SecretKind,
    SecretMetadata,
)
from offeragent_harness.protocol.messages import (
    SecretsDeleteParams,
    SecretsDeleteResult,
    SecretsListParams,
    SecretsListResult,
    SecretsPutParams,
    SecretsPutResult,
)
from offeragent_harness.runtime.application_domain_handlers import DomainCommandIdentity, secret_command_handlers
from offeragent_harness.testing import ManualCancellationToken

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
T = TypeVar("T")


class Store:
    def __init__(self) -> None:
        self.value: SecretMetadata | None = None
        self.observed_lengths: list[int] = []

    def create(self, *, scope_id: str, kind: SecretKind, provider_id: str, secret: SecretInput) -> SecretMetadata:
        plaintext = secret.take()
        try:
            self.observed_lengths.append(len(plaintext))
        finally:
            for index in range(len(plaintext)):
                plaintext[index] = 0
        self.value = SecretMetadata(SecretHandle("secret:v1:" + "a" * 32), scope_id, kind, provider_id, 1, NOW, NOW)
        return self.value

    def rotate(
        self,
        handle: SecretHandle,
        *,
        scope_id: str,
        expected_version: int,
        secret: SecretInput,
    ) -> SecretMetadata:
        assert self.value is not None and handle == self.value.handle and expected_version == self.value.version
        plaintext = secret.take()
        try:
            self.observed_lengths.append(len(plaintext))
        finally:
            for index in range(len(plaintext)):
                plaintext[index] = 0
        self.value = SecretMetadata(handle, scope_id, self.value.kind, self.value.provider_id, 2, NOW, NOW)
        return self.value

    def metadata(self, handle: SecretHandle, *, scope_id: str) -> SecretMetadata:
        assert self.value is not None and self.value.handle == handle and self.value.scope_id == scope_id
        return self.value

    def list_metadata(self, *, scope_id: str) -> tuple[SecretMetadata, ...]:
        return () if self.value is None or self.value.scope_id != scope_id else (self.value,)

    def delete(self, handle: SecretHandle, *, scope_id: str, expected_version: int) -> None:
        assert self.value is not None and self.value.handle == handle and self.value.scope_id == scope_id
        assert self.value.version == expected_version
        self.value = None

    def consume(
        self,
        handle: SecretHandle,
        *,
        scope_id: str,
        expected_kind: SecretKind,
        expected_provider_id: str,
        consumer: Callable[[memoryview], T],
    ) -> T:
        del handle, scope_id, expected_kind, expected_provider_id, consumer
        raise AssertionError("not used")


@pytest.mark.asyncio
async def test_secret_commands_return_only_opaque_metadata_and_rotate_with_cas() -> None:
    store = Store()
    identity = DomainCommandIdentity("ws_1", "profile_1", "managed", "actor_1")
    handlers = secret_command_handlers(identity=identity, secrets=store)
    context = ApplicationCommandContext(transport="stdio", client_id="plugin_1")
    cancellation = ManualCancellationToken()

    created = await handlers["secrets/put"](
        SecretsPutParams(provider_id="openai", kind="model-provider", secret=SecretStr("super-secret")),
        cancellation,
        context,
    )
    assert isinstance(created, SecretsPutResult)
    handle = created.secret.handle
    assert handle.startswith("secret:v1:") and "super-secret" not in repr(created)
    listed = await handlers["secrets/list"](SecretsListParams(), cancellation, context)
    assert isinstance(listed, SecretsListResult)
    assert listed.secrets[0].handle == handle
    rotated = await handlers["secrets/put"](
        SecretsPutParams(
            provider_id="openai",
            kind="model-provider",
            secret=SecretStr("rotated-secret"),
            handle=handle,
            expected_version=1,
        ),
        cancellation,
        context,
    )
    assert isinstance(rotated, SecretsPutResult)
    assert rotated.secret.version == 2 and store.observed_lengths == [12, 14]
    deleted = await handlers["secrets/delete"](
        SecretsDeleteParams(handle=handle, expected_version=2),
        cancellation,
        context,
    )
    assert isinstance(deleted, SecretsDeleteResult)
    assert deleted.deleted and store.value is None
