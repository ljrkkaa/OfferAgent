from __future__ import annotations

import concurrent.futures
import ctypes
import json
import os
import re
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from offeragent_harness.ports.secrets import SecretInput, SecretKind, SecretResolver, SecretStore
from offeragent_harness.runtime.windows_secrets import (
    DpapiCurrentUserProtector,
    SecretBindingMismatch,
    SecretConsumerError,
    SecretCorrupt,
    SecretDecryptionError,
    SecretNotFound,
    SecretVersionConflict,
    WindowsDpapiSecretStore,
)
from offeragent_harness.runtime.windows_security import (
    current_windows_identity,
    kernel_handle_is_inheritable,
    kernel_object_security_sddl,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires Windows DPAPI and ACLs")

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
_READ_CONTROL = 0x00020000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_FILE_SHARE_DELETE = 0x4
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000


def store(tmp_path: Path) -> WindowsDpapiSecretStore:
    values = iter(
        (
            uuid.UUID("12345678-1234-4234-8234-123456789abc"),
            uuid.UUID("22345678-1234-4234-8234-123456789abc"),
        )
    )
    times = iter((NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)))
    return WindowsDpapiSecretStore(tmp_path / "secrets", now=lambda: next(times), new_uuid=lambda: next(values))


def test_secret_crud_rotation_scope_isolation_and_plaintext_never_hits_disk(tmp_path: Path) -> None:
    secrets = store(tmp_path)
    assert isinstance(secrets, SecretStore)
    assert isinstance(secrets, SecretResolver)
    plaintext = "sk-super-secret-OfferAgent"
    original_env = dict(os.environ)
    original_argv = tuple(sys.argv)
    input_value = SecretInput(plaintext)

    created = secrets.create(
        scope_id="wsi_first",
        kind=SecretKind.MODEL_PROVIDER,
        provider_id="openai",
        secret=input_value,
    )

    assert "super-secret" not in repr(input_value)
    assert "super-secret" not in repr(created)
    assert secrets.consume(
        created.handle,
        scope_id="wsi_first",
        expected_kind=SecretKind.MODEL_PROVIDER,
        expected_provider_id="openai",
        consumer=lambda value: bytes(value) == plaintext.encode(),
    )
    with pytest.raises(SecretNotFound):
        secrets.consume(
            created.handle,
            scope_id="wsi_other",
            expected_kind=SecretKind.MODEL_PROVIDER,
            expected_provider_id="openai",
            consumer=lambda value: True,
        )
    assert secrets.list_metadata(scope_id="wsi_first") == (created,)
    assert secrets.list_metadata(scope_id="wsi_other") == ()
    files = list(secrets.root.glob("*.secret"))
    assert len(files) == 1
    assert created.handle.opaque_id not in files[0].name
    disk = b"".join(path.read_bytes() for path in secrets.root.iterdir() if path.is_file())
    assert plaintext.encode() not in disk
    assert original_env == dict(os.environ)
    assert original_argv == tuple(sys.argv)

    rotated = secrets.rotate(
        created.handle,
        scope_id="wsi_first",
        expected_version=1,
        secret=SecretInput("rotated-secret"),
    )
    assert rotated.version == 2
    assert secrets.consume(
        created.handle,
        scope_id="wsi_first",
        expected_kind=SecretKind.MODEL_PROVIDER,
        expected_provider_id="openai",
        consumer=lambda value: bytes(value) == b"rotated-secret",
    )
    assert plaintext.encode() not in files[0].read_bytes()
    with pytest.raises(SecretVersionConflict):
        secrets.delete(created.handle, scope_id="wsi_first", expected_version=1)
    secrets.delete(created.handle, scope_id="wsi_first", expected_version=2)
    with pytest.raises(SecretNotFound):
        secrets.metadata(created.handle, scope_id="wsi_first")


def test_consumer_cannot_keep_a_live_plaintext_view(tmp_path: Path) -> None:
    secrets = store(tmp_path)
    created = secrets.create(
        scope_id="wsi_first",
        kind=SecretKind.MODEL_PROVIDER,
        provider_id="github",
        secret=SecretInput("temporary-value"),
    )
    escaped: list[memoryview] = []

    def capture(value: memoryview) -> None:
        escaped.append(value)

    secrets.consume(
        created.handle,
        scope_id="wsi_first",
        expected_kind=SecretKind.MODEL_PROVIDER,
        expected_provider_id="github",
        consumer=capture,
    )
    with pytest.raises(ValueError, match="released memoryview"):
        escaped[0].tobytes()

    with pytest.raises(SecretConsumerError, match="cannot return"):
        secrets.consume(
            created.handle,
            scope_id="wsi_first",
            expected_kind=SecretKind.MODEL_PROVIDER,
            expected_provider_id="github",
            consumer=lambda value: bytes(value),
        )

    with pytest.raises(SecretConsumerError) as captured:
        secrets.consume(
            created.handle,
            scope_id="wsi_first",
            expected_kind=SecretKind.MODEL_PROVIDER,
            expected_provider_id="github",
            consumer=lambda value: (_ for _ in ()).throw(RuntimeError(bytes(value))),
        )
    assert "temporary-value" not in str(captured.value)


@pytest.mark.parametrize(
    ("kind", "provider_id"),
    [
        (SecretKind.MODEL_PROVIDER, "openai-compatible.3998a78273bce9bc858e8a81fa76c1ea"),
        (SecretKind.MODEL_PROVIDER, "codex"),
        (SecretKind.UPDATE, "openai-compatible.74e02ac9ca3e818e94a4a9a7ed86be3f"),
    ],
)
def test_consume_rejects_wrong_endpoint_provider_or_kind_before_plaintext(
    tmp_path: Path,
    kind: SecretKind,
    provider_id: str,
) -> None:
    secrets = store(tmp_path)
    created = secrets.create(
        scope_id="wsi_first",
        kind=SecretKind.MODEL_PROVIDER,
        provider_id="openai-compatible.74e02ac9ca3e818e94a4a9a7ed86be3f",
        secret=SecretInput("endpoint-a-only"),
    )
    consumed = False

    def capture(_value: memoryview) -> None:
        nonlocal consumed
        consumed = True

    with pytest.raises(SecretBindingMismatch, match="expected consumer"):
        secrets.consume(
            created.handle,
            scope_id="wsi_first",
            expected_kind=kind,
            expected_provider_id=provider_id,
            consumer=capture,
        )
    assert not consumed


def test_official_openai_secret_cannot_be_consumed_by_codex(tmp_path: Path) -> None:
    secrets = store(tmp_path)
    created = secrets.create(
        scope_id="wsi_first",
        kind=SecretKind.MODEL_PROVIDER,
        provider_id="openai",
        secret=SecretInput("official-openai-only"),
    )

    with pytest.raises(SecretBindingMismatch):
        secrets.consume(
            created.handle,
            scope_id="wsi_first",
            expected_kind=SecretKind.MODEL_PROVIDER,
            expected_provider_id="codex",
            consumer=lambda _value: None,
        )


def test_dpapi_is_current_user_and_entropy_bound() -> None:
    protector = DpapiCurrentUserProtector()
    plaintext = bytearray(b"account-bound-secret")
    ciphertext = protector.protect(plaintext, entropy=b"correct-entropy")

    assert b"account-bound-secret" not in ciphertext
    assert protector.unprotect(ciphertext, entropy=b"correct-entropy") == plaintext
    with pytest.raises(SecretDecryptionError):
        protector.unprotect(ciphertext, entropy=b"wrong-entropy")


def test_corrupt_envelope_fails_closed_without_deletion(tmp_path: Path) -> None:
    secrets = store(tmp_path)
    created = secrets.create(
        scope_id="wsi_first",
        kind=SecretKind.UPDATE,
        provider_id="search",
        secret=SecretInput("search-token"),
    )
    path = next(secrets.root.glob("*.secret"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["ciphertext"] = raw["ciphertext"][:-4] + "AAAA"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SecretCorrupt):
        secrets.metadata(created.handle, scope_id="wsi_first")
    assert path.exists()


def test_concurrent_rotation_expected_version_has_exactly_one_winner(tmp_path: Path) -> None:
    secrets = store(tmp_path)
    created = secrets.create(
        scope_id="wsi_first",
        kind=SecretKind.MODEL_PROVIDER,
        provider_id="openai",
        secret=SecretInput("initial"),
    )

    def rotate(index: int) -> object:
        try:
            return secrets.rotate(
                created.handle,
                scope_id="wsi_first",
                expected_version=1,
                secret=SecretInput(f"rotated-{index}"),
            )
        except SecretVersionConflict as error:
            return error

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(rotate, range(8)))

    assert sum(not isinstance(item, BaseException) for item in results) == 1
    assert sum(isinstance(item, SecretVersionConflict) for item in results) == 7
    assert secrets.metadata(created.handle, scope_id="wsi_first").version == 2


def test_cross_process_rotation_mutex_and_file_cas_have_one_winner(tmp_path: Path) -> None:
    secrets = store(tmp_path)
    created = secrets.create(
        scope_id="wsi_first",
        kind=SecretKind.MODEL_PROVIDER,
        provider_id="openai",
        secret=SecretInput("initial"),
    )
    trigger = tmp_path / "rotate.trigger"
    script = r"""
import os
import sys
import time
from pathlib import Path
from offeragent_harness.ports.secrets import SecretHandle, SecretInput
from offeragent_harness.runtime.windows_secrets import SecretVersionConflict, WindowsDpapiSecretStore
root, handle, scope, trigger = sys.argv[1:]
while not Path(trigger).exists():
    time.sleep(0.01)
store = WindowsDpapiSecretStore(Path(root))
try:
    store.rotate(
        SecretHandle(handle),
        scope_id=scope,
        expected_version=1,
        secret=SecretInput(f"generated-in-child-{os.getpid()}"),
    )
except SecretVersionConflict:
    raise SystemExit(17)
raise SystemExit(0)
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(secrets.root),
                created.handle.opaque_id,
                "wsi_first",
                str(trigger),
            ],
            close_fds=True,
            creationflags=0x08000000,
        )
        for _ in range(2)
    ]
    time.sleep(0.1)
    trigger.write_text("go", encoding="ascii")
    return_codes = sorted(process.wait(timeout=15) for process in processes)

    assert return_codes == [0, 17]
    assert secrets.metadata(created.handle, scope_id="wsi_first").version == 2


def test_store_directory_and_secret_file_have_current_sid_only_dacl(tmp_path: Path) -> None:
    secrets = store(tmp_path)
    secrets.create(
        scope_id="wsi_first",
        kind=SecretKind.MODEL_PROVIDER,
        provider_id="openai",
        secret=SecretInput("acl-secret"),
    )
    file_path = next(secrets.root.glob("*.secret"))
    expected_sid = current_windows_identity().sid
    for path, directory in ((secrets.root, True), (file_path, False)):
        handle = _open_security_handle(path, directory=directory)
        try:
            sddl = kernel_object_security_sddl(handle)
            trustees = re.findall(r"\([^)]*;;;([^)]+)\)", sddl)
            assert trustees == [expected_sid]
            assert all(alias not in trustees for alias in ("SY", "BA", "WD", "AU"))
            assert not kernel_handle_is_inheritable(handle)
        finally:
            _close_handle(handle)


def _open_security_handle(path: Path, *, directory: bool) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        str(path),
        _READ_CONTROL,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS if directory else 0,
        None,
    )
    if not handle or int(handle) == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        raise OSError(code, ctypes.FormatError(code))
    return int(handle)


def _close_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.CloseHandle(wintypes.HANDLE(handle)):
        code = ctypes.get_last_error()
        raise OSError(code, ctypes.FormatError(code))
