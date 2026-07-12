import ctypes
import difflib
import hashlib
import json
import logging
import os
import re
import tempfile
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Union
from uuid import UUID, uuid4

from django.db import connection, transaction
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from khoj.database.models import Conversation, VaultActionBatch
from khoj.utils.local_kb import get_local_kb_root

MAX_ACTIONS = 20
MAX_ACTION_CONTENT_BYTES = 1024 * 1024
MAX_BATCH_CONTENT_BYTES = 5 * 1024 * 1024
MAX_TARGET_BYTES = 10 * 1024 * 1024
MAX_PREVIEW_BYTES = 200 * 1024
BATCH_TTL = timedelta(minutes=30)
ALLOWED_SUFFIXES = {".md", ".txt"}

logger = logging.getLogger(__name__)


class VaultActionError(ValueError):
    pass


class VaultActionTurnConflict(VaultActionError):
    def __init__(self, batch: VaultActionBatch):
        self.batch = batch
        super().__init__("This turn already has a different vault action batch.")


class CreateFileAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    op: Literal["create_file"]
    path: str = Field(min_length=1)
    content: str
    mode: Literal["create_only"]


class AppendFileAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    op: Literal["append_file"]
    path: str = Field(min_length=1)
    content: str
    heading: str | None = None
    mode: Literal["append"]


class ReplaceTextAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    op: Literal["replace_text"]
    path: str = Field(min_length=1)
    find: str = Field(min_length=1)
    replace: str
    mode: Literal["replace"]
    reason: str | None = None


VaultAction = Annotated[
    Union[CreateFileAction, AppendFileAction, ReplaceTextAction],
    Field(discriminator="op"),
]
VAULT_ACTIONS_ADAPTER = TypeAdapter(list[VaultAction])


@dataclass(frozen=True)
class PreparedVaultBatch:
    actions: list[dict[str, Any]]
    snapshots: dict[str, dict[str, Any]]
    previews: list[dict[str, Any]]
    final_contents: dict[str, str]


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _root_fingerprint(root: Path) -> str:
    resolved = root.resolve(strict=True)
    stat = resolved.stat()
    identity = f"{resolved}\0{stat.st_dev}\0{stat.st_ino}"
    return _sha256(identity)


def _action_digest(actions: list[dict[str, Any]]) -> str:
    canonical = json.dumps(actions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256(canonical)


def _advisory_lock_key(root_fingerprint: str) -> int:
    value = int(root_fingerprint[:16], 16)
    return value - 2**64 if value >= 2**63 else value


@contextmanager
def _vault_advisory_lock(root_fingerprint: str):
    if connection.vendor != "postgresql":
        raise VaultActionError("VaultAction requires PostgreSQL advisory locks.")
    key = _advisory_lock_key(root_fingerprint)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [key])
    try:
        yield
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
        except Exception:
            logger.exception("Failed to explicitly release VaultAction advisory lock")


def web_vault_write_enabled() -> bool:
    enabled = os.getenv("KHOJ_ALLOW_VAULT_WRITE", "false").strip().lower() in {"1", "true", "yes", "on"}
    return enabled and get_local_kb_root() is not None


def _safe_target(root: Path, raw_path: str) -> tuple[str, Path]:
    value = raw_path.strip().replace("\\", "/")
    if not value or value.startswith("/") or value.endswith("/") or re.match(r"^[A-Za-z]:", value):
        raise VaultActionError(f"Unsafe vault action path: {raw_path}")
    relative = PurePosixPath(value)
    if any(part in {"", ".", ".."} or part.startswith(".") for part in relative.parts):
        raise VaultActionError(f"Unsafe vault action path: {raw_path}")
    if relative.suffix.lower() not in ALLOWED_SUFFIXES:
        raise VaultActionError(f"Unsupported vault action file type: {raw_path}")

    safe_root = root.resolve(strict=True)
    target = safe_root.joinpath(*relative.parts)
    resolved = target.resolve(strict=False)
    if not resolved.is_relative_to(safe_root):
        raise VaultActionError(f"Vault action path escapes the configured root: {raw_path}")
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise VaultActionError(f"Vault action target is not a regular file: {raw_path}")
    return relative.as_posix(), target


def _bounded_diff(path: str, before: str, after: str) -> tuple[str, bool]:
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    encoded = diff.encode("utf-8")
    if len(encoded) <= MAX_PREVIEW_BYTES:
        return diff, False
    return encoded[:MAX_PREVIEW_BYTES].decode("utf-8", errors="ignore"), True


def _append_content(existing: str, content: str, heading: str | None = None) -> str:
    normalized = content.rstrip("\n") + "\n"
    if not heading:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        return existing + separator + normalized

    lines = existing.split("\n")
    pattern = re.compile(rf"^(#{{1,6}})\s+{re.escape(heading)}\s*$")
    heading_index = next((index for index, line in enumerate(lines) if pattern.match(line)), None)
    if heading_index is None:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        return f"{existing}{separator}\n## {heading}\n\n{normalized}"

    heading_level = len(pattern.match(lines[heading_index]).group(1))
    insert_index = len(lines)
    for index in range(heading_index + 1, len(lines)):
        match = re.match(r"^(#{1,6})\s+", lines[index])
        if match and len(match.group(1)) <= heading_level:
            insert_index = index
            break

    before = lines[:insert_index]
    if before and before[-1].strip():
        before.append("")
    result = "\n".join([*before, *normalized.rstrip("\n").split("\n"), *lines[insert_index:]])
    return result if result.endswith("\n") else result + "\n"


def _validated_actions(raw_actions: list[dict[str, Any]]) -> list[VaultAction]:
    if not isinstance(raw_actions, list) or not raw_actions or len(raw_actions) > MAX_ACTIONS:
        raise VaultActionError(f"Vault action batch must contain between 1 and {MAX_ACTIONS} actions.")
    try:
        actions = VAULT_ACTIONS_ADAPTER.validate_python(raw_actions, strict=True)
    except ValidationError as error:
        raise VaultActionError(f"Invalid vault action batch: {error}") from None

    total_bytes = 0
    for action in actions:
        payload = (
            action.content if isinstance(action, (CreateFileAction, AppendFileAction)) else action.find + action.replace
        )
        payload_bytes = len(payload.encode("utf-8"))
        if payload_bytes > MAX_ACTION_CONTENT_BYTES:
            raise VaultActionError(f"Vault action content exceeds {MAX_ACTION_CONTENT_BYTES} bytes: {action.path}")
        total_bytes += payload_bytes
    if total_bytes > MAX_BATCH_CONTENT_BYTES:
        raise VaultActionError(f"Vault action batch content exceeds {MAX_BATCH_CONTENT_BYTES} bytes.")
    return actions


def prepare_vault_action_batch(root: Path, raw_actions: list[dict[str, Any]]) -> PreparedVaultBatch:
    actions = _validated_actions(raw_actions)
    snapshots: dict[str, dict[str, Any]] = {}
    originals: dict[str, str] = {}
    final_contents: dict[str, str] = {}
    current_exists: dict[str, bool] = {}

    for action in actions:
        path, target = _safe_target(root, action.path)
        if path not in snapshots:
            exists = target.exists()
            if exists and target.stat().st_size > MAX_TARGET_BYTES:
                raise VaultActionError(f"Vault action target exceeds {MAX_TARGET_BYTES} bytes: {path}")
            original = target.read_text(encoding="utf-8") if exists else ""
            originals[path] = original
            final_contents[path] = original
            current_exists[path] = exists
            snapshots[path] = {"exists": exists, "sha256": _sha256(original) if exists else None}

        if isinstance(action, CreateFileAction):
            if current_exists[path]:
                raise VaultActionError(f"File already exists: {path}")
            final_contents[path] = action.content
            current_exists[path] = True
        elif isinstance(action, AppendFileAction):
            if not current_exists[path]:
                raise VaultActionError(f"File does not exist: {path}")
            final_contents[path] = _append_content(final_contents[path], action.content, action.heading)
        elif isinstance(action, ReplaceTextAction):
            if not current_exists[path]:
                raise VaultActionError(f"File does not exist: {path}")
            matches = final_contents[path].count(action.find)
            if matches != 1:
                raise VaultActionError(f"replace_text expected exactly one match in {path}, found {matches}")
            final_contents[path] = final_contents[path].replace(action.find, action.replace, 1)

    previews = []
    for path, after in final_contents.items():
        diff, truncated = _bounded_diff(path, originals[path], after)
        op = next(action.op for action in actions if _safe_target(root, action.path)[0] == path)
        previews.append({"op": op, "path": path, "diff": diff, "truncated": truncated})

    return PreparedVaultBatch(
        actions=[action.model_dump(mode="json", exclude_none=True) for action in actions],
        snapshots=snapshots,
        previews=previews,
        final_contents=final_contents,
    )


def create_vault_action_batch(*, user, conversation, turn_id, actions: list[dict[str, Any]]) -> VaultActionBatch:
    if not web_vault_write_enabled():
        raise VaultActionError("Web vault writing is disabled.")
    if conversation.user_id != user.id:
        raise VaultActionError("Conversation does not belong to the current user.")
    try:
        normalized_turn_id = UUID(str(turn_id))
    except (TypeError, ValueError):
        raise VaultActionError("Vault action turn_id must be a UUID.") from None
    root = get_local_kb_root()
    if root is None:
        raise VaultActionError("Local knowledge base is not configured.")
    root_fingerprint = _root_fingerprint(root)
    normalized_actions = [action.model_dump(mode="json", exclude_none=True) for action in _validated_actions(actions)]
    action_digest = _action_digest(normalized_actions)
    with _vault_advisory_lock(root_fingerprint):
        with transaction.atomic():
            locked_conversation = Conversation.objects.select_for_update().filter(id=conversation.id, user=user).first()
            if locked_conversation is None:
                raise VaultActionError("Conversation does not belong to the current user.")
            existing = VaultActionBatch.objects.filter(
                conversation=locked_conversation,
                turn_id=normalized_turn_id,
            ).first()
            if existing is not None:
                if (
                    existing.root_fingerprint == root_fingerprint
                    and existing.action_digest == action_digest
                    and existing.actions == normalized_actions
                ):
                    return existing
                raise VaultActionTurnConflict(existing)
            prepared = prepare_vault_action_batch(root, normalized_actions)
            return VaultActionBatch.objects.create(
                user=user,
                conversation=locked_conversation,
                turn_id=normalized_turn_id,
                actions=prepared.actions,
                snapshots=prepared.snapshots,
                previews=prepared.previews,
                root_fingerprint=root_fingerprint,
                action_digest=action_digest,
                expires_at=timezone.now() + BATCH_TTL,
            )


def _snapshot_target(root: Path, path: str) -> tuple[Path, dict[str, Any], str]:
    relative, target = _safe_target(root, path)
    exists = target.exists()
    if exists and target.stat().st_size > MAX_TARGET_BYTES:
        raise VaultActionError(f"Vault action target exceeds {MAX_TARGET_BYTES} bytes: {relative}")
    content = target.read_text(encoding="utf-8") if exists else ""
    return target, {"exists": exists, "sha256": _sha256(content) if exists else None}, content


def _created_parent_dirs(root: Path, target: Path) -> list[str]:
    missing: list[str] = []
    current = target.parent
    while current != root:
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise VaultActionError(f"Vault action parent is not a safe directory: {current.relative_to(root)}")
            break
        missing.append(current.relative_to(root).as_posix())
        current = current.parent
    return list(reversed(missing))


def _build_rollback_journal(root: Path, prepared: PreparedVaultBatch, recovery_id: str) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    created_dirs: list[str] = []
    for path, final_content in prepared.final_contents.items():
        target, snapshot, original = _snapshot_target(root, path)
        files[path] = {
            "exists": snapshot["exists"],
            "original": original if snapshot["exists"] else None,
            "original_sha256": snapshot["sha256"],
            "final_sha256": _sha256(final_content),
        }
        for directory in _created_parent_dirs(root, target):
            if directory not in created_dirs:
                created_dirs.append(directory)
    return {"files": files, "created_dirs": created_dirs, "recovery_id": recovery_id}


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_file_version(target: Path) -> tuple[bool, str | None, tuple[int, int, int, int, int] | None]:
    if target.is_symlink():
        return True, "unsafe", None
    if not target.exists():
        return False, None, None
    try:
        before = target.stat()
        if not target.is_file() or before.st_size > MAX_TARGET_BYTES:
            return True, "unsafe", None
        content = target.read_text(encoding="utf-8")
        after = target.stat()
    except (OSError, UnicodeError):
        return True, "unstable", None
    before_token = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    after_token = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if before_token != after_token:
        return True, "unstable", None
    return True, _sha256(content), after_token


AT_FDCWD = -100
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2


def _renameat2(source: Path, destination: Path, flags: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise VaultActionError("This filesystem runtime does not support atomic VaultAction exchanges.")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), f"{source} -> {destination}")


def _exchange_paths(left: Path, right: Path) -> None:
    _renameat2(left, right, RENAME_EXCHANGE)


def _move_no_replace(source: Path, destination: Path) -> None:
    _renameat2(source, destination, RENAME_NOREPLACE)


def _preserve_recovery_artifact(
    source: Path,
    target: Path,
    purpose: str,
    recovery_id: str | None = None,
) -> Path:
    identifier = recovery_id or uuid4().hex
    artifact = target.parent / f".offeragent-recovery-{identifier}-{purpose}-{uuid4().hex}"
    _move_no_replace(source, artifact)
    _fsync_directory(target.parent)
    return artifact


def _atomic_write_text(
    target: Path,
    content: str,
    *,
    expected: dict[str, Any] | None = None,
    recovery_id: str | None = None,
) -> Path | None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    temporary_is_generated = True
    recovery_artifact: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".offeragent-recovery-{recovery_id}-working-" if recovery_id else ".offeragent-tmp-",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name

        exists, checksum, _ = _read_file_version(target)
        actual = {"exists": exists, "sha256": checksum if exists else None}
        if expected is not None and actual != expected:
            raise VaultActionError(f"Vault file changed during apply: {target.name}")

        if not exists:
            try:
                os.link(temporary_name, target)
            except FileExistsError:
                raise VaultActionError(f"Vault file appeared during create: {target.name}") from None
            Path(temporary_name).unlink()
            temporary_name = ""
        else:
            original_mode = target.stat().st_mode
            os.chmod(temporary_name, original_mode)
            temporary_path = Path(temporary_name)
            _exchange_paths(temporary_path, target)
            temporary_is_generated = False

            captured_exists, captured_checksum, _ = _read_file_version(temporary_path)
            captured = {
                "exists": captured_exists,
                "sha256": captured_checksum if captured_exists else None,
            }
            if expected is not None and captured != expected:
                try:
                    _exchange_paths(temporary_path, target)
                except Exception as exchange_error:
                    recovery_artifact = _preserve_recovery_artifact(
                        temporary_path,
                        target,
                        "exchange-failed",
                        recovery_id,
                    )
                    temporary_name = ""
                    raise VaultActionError(
                        "Vault file changed during atomic apply and automatic restoration failed; "
                        f"preserved recovery artifact: {recovery_artifact.name}"
                    ) from exchange_error

                recovery_artifact = _preserve_recovery_artifact(
                    temporary_path,
                    target,
                    "rejected",
                    recovery_id,
                )
                temporary_name = ""
                raise VaultActionError(
                    "Vault file changed during atomic apply; the external version was restored and "
                    f"the displaced candidate was preserved as {recovery_artifact.name}"
                )

            recovery_artifact = _preserve_recovery_artifact(
                temporary_path,
                target,
                "displaced",
                recovery_id,
            )
            temporary_name = ""

        _fsync_directory(target.parent)
        final_exists, final_checksum, _ = _read_file_version(target)
        if not final_exists or final_checksum != _sha256(content):
            raise VaultActionError(f"Vault file changed before apply completed: {target.name}")
        return recovery_artifact
    finally:
        if temporary_name:
            temporary_path = Path(temporary_name)
            if temporary_is_generated:
                temporary_path.unlink(missing_ok=True)
            else:
                try:
                    _preserve_recovery_artifact(temporary_path, target, "unresolved", recovery_id)
                except Exception:
                    logger.exception(
                        "Could not move unresolved VaultAction file %s; leaving it in place",
                        temporary_path,
                    )


def _commit_prepared(root: Path, prepared: PreparedVaultBatch, *, recovery_id: str | None = None) -> list[str]:
    recovery_artifacts: list[str] = []
    for path in sorted(prepared.final_contents):
        _, target = _safe_target(root, path)
        artifact = _atomic_write_text(
            target,
            prepared.final_contents[path],
            expected=prepared.snapshots[path],
            recovery_id=recovery_id,
        )
        if artifact is not None:
            recovery_artifacts.append(artifact.relative_to(root).as_posix())
    return recovery_artifacts


def _current_hash(target: Path) -> tuple[bool, str | None]:
    exists, checksum, _ = _read_file_version(target)
    return exists, checksum


def _atomic_delete_if_matches(
    target: Path,
    expected_hash: str,
    *,
    recovery_id: str | None = None,
) -> Path:
    identifier = recovery_id or uuid4().hex
    recovery_artifact = target.parent / f".offeragent-recovery-{identifier}-rollback-{uuid4().hex}"
    _move_no_replace(target, recovery_artifact)
    exists, checksum = _current_hash(recovery_artifact)
    if not exists or checksum != expected_hash:
        try:
            os.link(recovery_artifact, target, follow_symlinks=False)
        except FileExistsError:
            raise VaultActionError(
                f"File changed during rollback and was preserved as {recovery_artifact.name}"
            ) from None
        raise VaultActionError(f"File changed during rollback; restored it and preserved {recovery_artifact.name}")
    _fsync_directory(target.parent)
    if target.exists() or target.is_symlink():
        raise VaultActionError(
            f"A new file appeared during rollback; preserved the batch file as {recovery_artifact.name}"
        )
    return recovery_artifact


def _find_recovery_artifacts(root: Path, journal: dict[str, Any]) -> list[str]:
    recovery_id = journal.get("recovery_id")
    files = journal.get("files") if isinstance(journal.get("files"), dict) else {}
    if not isinstance(recovery_id, str) or not recovery_id or not files:
        return sorted(set(journal.get("recovery_artifacts") or []))

    artifacts = set(journal.get("recovery_artifacts") or [])
    resolved_root = root.resolve(strict=True)
    for path in files:
        _, target = _safe_target(root, path)
        for artifact in target.parent.glob(f".offeragent-recovery-{recovery_id}-*"):
            if artifact.exists() or artifact.is_symlink():
                artifacts.add(artifact.relative_to(resolved_root).as_posix())
    return sorted(artifacts)


def _rollback_files(root: Path, journal: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    files = journal.get("files") if isinstance(journal.get("files"), dict) else {}
    recovery_id = journal.get("recovery_id") if isinstance(journal.get("recovery_id"), str) else None
    for path in sorted(files, reverse=True):
        entry = files[path]
        try:
            _, target = _safe_target(root, path)
            exists, checksum = _current_hash(target)
            current_state = (exists, checksum)
            original_state = (bool(entry.get("exists")), entry.get("original_sha256"))
            final_state = (True, entry.get("final_sha256"))
            if current_state not in {original_state, final_state}:
                raise VaultActionError(f"File changed during rollback: {path}")
            if current_state == original_state:
                continue
            if entry.get("exists"):
                artifact = _atomic_write_text(
                    target,
                    str(entry.get("original") or ""),
                    expected={"exists": True, "sha256": entry.get("final_sha256")},
                    recovery_id=recovery_id,
                )
            else:
                artifact = _atomic_delete_if_matches(
                    target,
                    str(entry.get("final_sha256") or ""),
                    recovery_id=recovery_id,
                )
            if artifact is not None:
                journal.setdefault("recovery_artifacts", []).append(artifact.relative_to(root).as_posix())
        except Exception as error:
            errors.append(f"{path}: {error}")

    for directory in reversed(journal.get("created_dirs") or []):
        try:
            target = root / directory
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()
        except Exception as error:
            errors.append(f"{directory}: {error}")
    return errors


def apply_vault_action_batch(batch_id, user) -> VaultActionBatch:
    if not web_vault_write_enabled():
        raise VaultActionError("Web vault writing is disabled.")
    root = get_local_kb_root()
    if root is None:
        raise VaultActionError("Local knowledge base is not configured.")
    identity = VaultActionBatch.objects.only("root_fingerprint").get(id=batch_id, user=user)

    with _vault_advisory_lock(identity.root_fingerprint):
        with transaction.atomic():
            batch = VaultActionBatch.objects.select_for_update().get(id=batch_id, user=user)
            if batch.status == VaultActionBatch.Status.APPLIED:
                return batch
            if batch.status != VaultActionBatch.Status.PENDING:
                raise VaultActionError(f"Vault action batch cannot be applied from status {batch.status}.")
            if batch.root_fingerprint != _root_fingerprint(root):
                batch.status = VaultActionBatch.Status.CONFLICT
                batch.result = {"error": "Configured vault changed after this batch was prepared."}
                batch.save(update_fields=["status", "result", "updated_at"])
                return batch
            if batch.expires_at <= timezone.now():
                batch.status = VaultActionBatch.Status.EXPIRED
                batch.result = {"error": "Vault action batch expired before it was applied."}
                batch.save(update_fields=["status", "result", "updated_at"])
                return batch

            for path, expected in batch.snapshots.items():
                _, actual, _ = _snapshot_target(root, path)
                if actual != expected:
                    batch.status = VaultActionBatch.Status.CONFLICT
                    batch.result = {"error": f"Vault file changed before apply: {path}", "files": [path]}
                    batch.save(update_fields=["status", "result", "updated_at"])
                    return batch

            prepared = prepare_vault_action_batch(root, batch.actions)
            journal = _build_rollback_journal(root, prepared, str(batch.id))
            batch.status = VaultActionBatch.Status.APPLYING
            batch.rollback_journal = journal
            batch.save(update_fields=["status", "rollback_journal", "updated_at"])

        with transaction.atomic():
            batch = VaultActionBatch.objects.select_for_update().get(id=batch_id, user=user)
            if batch.status != VaultActionBatch.Status.APPLYING:
                return batch
            try:
                _commit_prepared(root, prepared, recovery_id=journal["recovery_id"])
                recovery_artifacts = _find_recovery_artifacts(root, journal)
            except Exception as error:
                rollback_errors = _rollback_files(root, journal)
                batch.status = (
                    VaultActionBatch.Status.MANUAL_REVIEW_REQUIRED
                    if rollback_errors
                    else VaultActionBatch.Status.FAILED
                )
                batch.result = {
                    "error": str(error),
                    "rollback_errors": rollback_errors,
                    "files": sorted(journal["files"]),
                    "recovery_artifacts": _find_recovery_artifacts(root, journal),
                }
                if not rollback_errors:
                    batch.rollback_journal = {}
                batch.save(update_fields=["status", "result", "rollback_journal", "updated_at"])
                return batch

            batch.status = VaultActionBatch.Status.APPLIED
            batch.result = {
                "files": sorted(prepared.final_contents),
                "recovery_artifacts": sorted(set(recovery_artifacts)),
            }
            batch.rollback_journal = {}
            batch.save(update_fields=["status", "result", "rollback_journal", "updated_at"])
            return batch


def cancel_vault_action_batch(batch_id, user) -> VaultActionBatch:
    with transaction.atomic():
        batch = VaultActionBatch.objects.select_for_update().get(id=batch_id, user=user)
        if batch.status == VaultActionBatch.Status.CANCELLED:
            return batch
        if batch.status != VaultActionBatch.Status.PENDING:
            raise VaultActionError(f"Vault action batch cannot be cancelled from status {batch.status}.")
        if batch.expires_at <= timezone.now():
            batch.status = VaultActionBatch.Status.EXPIRED
            batch.result = {"error": "Vault action batch expired before it was cancelled."}
        else:
            batch.status = VaultActionBatch.Status.CANCELLED
            batch.result = {"files": sorted(batch.snapshots)}
        batch.save(update_fields=["status", "result", "updated_at"])
    return batch


def delete_conversations_with_vault_protection(*, user, conversation_id=None) -> tuple[int, dict[str, int]]:
    if conversation_id is not None:
        try:
            normalized_conversation_id = UUID(str(conversation_id))
        except (TypeError, ValueError):
            return 0, {}
        conversation_ids = list(
            Conversation.objects.filter(user=user, id=normalized_conversation_id).values_list("id", flat=True)
        )
    else:
        conversation_ids = list(Conversation.objects.filter(user=user).values_list("id", flat=True))
    if not conversation_ids:
        return 0, {}

    identities = list(
        VaultActionBatch.objects.filter(user=user, conversation_id__in=conversation_ids).only(
            "id", "status", "root_fingerprint", "user_id"
        )
    )
    fingerprints = {batch.root_fingerprint for batch in identities}
    root = get_local_kb_root()
    if root is not None:
        fingerprints.add(_root_fingerprint(root))

    with ExitStack() as locks:
        for fingerprint in sorted(fingerprints):
            locks.enter_context(_vault_advisory_lock(fingerprint))
        for batch in identities:
            if batch.status == VaultActionBatch.Status.APPLYING:
                recover_vault_action_batch(batch)

        with transaction.atomic():
            locked_conversations = list(
                Conversation.objects.select_for_update()
                .filter(user=user, id__in=conversation_ids)
                .values_list("id", flat=True)
            )
            if not locked_conversations:
                return 0, {}
            batches = VaultActionBatch.objects.select_for_update().filter(
                user=user,
                conversation_id__in=locked_conversations,
            )
            blocked_statuses = {
                VaultActionBatch.Status.APPLYING,
                VaultActionBatch.Status.CONFLICT,
                VaultActionBatch.Status.FAILED,
                VaultActionBatch.Status.MANUAL_REVIEW_REQUIRED,
            }
            blocked = list(batches.filter(status__in=blocked_statuses).values_list("status", flat=True))
            if blocked:
                statuses = ", ".join(sorted(set(blocked)))
                raise VaultActionError(
                    f"Conversation has unresolved vault action batches ({statuses}); review them before deletion."
                )
            batches.filter(status=VaultActionBatch.Status.PENDING).update(
                status=VaultActionBatch.Status.CANCELLED,
                result={"reason": "conversation_deleted"},
                updated_at=timezone.now(),
            )
            return Conversation.objects.filter(user=user, id__in=locked_conversations).delete()


def delete_conversation_turn_and_cancel_batch(*, user, conversation_id, turn_id) -> bool:
    try:
        normalized_conversation_id = UUID(str(conversation_id))
    except (TypeError, ValueError):
        return False
    try:
        normalized_turn_id = UUID(str(turn_id))
    except (TypeError, ValueError):
        normalized_turn_id = None

    identity = None
    if normalized_turn_id is not None:
        identity = (
            VaultActionBatch.objects.filter(
                user=user,
                conversation_id=normalized_conversation_id,
                turn_id=normalized_turn_id,
            )
            .only("id", "status", "root_fingerprint", "user_id")
            .first()
        )
    if identity is not None and identity.status == VaultActionBatch.Status.APPLYING:
        identity = recover_vault_action_batch(identity)

    lock_fingerprint = identity.root_fingerprint if identity is not None else None
    if lock_fingerprint is None:
        root = get_local_kb_root()
        if root is not None:
            lock_fingerprint = _root_fingerprint(root)
    lock = _vault_advisory_lock(lock_fingerprint) if lock_fingerprint else nullcontext()

    with lock, transaction.atomic():
        conversation = Conversation.objects.select_for_update().filter(id=normalized_conversation_id, user=user).first()
        if not conversation or not conversation.conversation_log or not conversation.conversation_log.get("chat"):
            return False
        chat = conversation.conversation_log["chat"]
        updated_chat = [message for message in chat if message.get("turnId") != turn_id]
        if len(updated_chat) == len(chat):
            return False

        if normalized_turn_id is not None:
            batch = (
                VaultActionBatch.objects.select_for_update()
                .filter(
                    user=user,
                    conversation=conversation,
                    turn_id=normalized_turn_id,
                )
                .first()
            )
            if batch is not None and batch.status == VaultActionBatch.Status.PENDING:
                batch.status = VaultActionBatch.Status.CANCELLED
                batch.result = {"files": sorted(batch.snapshots), "reason": "conversation_turn_deleted"}
                batch.save(update_fields=["status", "result", "updated_at"])

        conversation_log = dict(conversation.conversation_log)
        conversation_log["chat"] = updated_chat
        conversation.conversation_log = conversation_log
        conversation.save(update_fields=["conversation_log", "updated_at"])
        return True


def expire_vault_action_batch_if_pending(batch_id, user) -> VaultActionBatch:
    now = timezone.now()
    VaultActionBatch.objects.filter(
        id=batch_id,
        user=user,
        status=VaultActionBatch.Status.PENDING,
        expires_at__lte=now,
    ).update(
        status=VaultActionBatch.Status.EXPIRED,
        result={"error": "Vault action batch expired before review."},
        updated_at=now,
    )
    return VaultActionBatch.objects.select_related("user").get(id=batch_id, user=user)


def recover_vault_action_batch(batch: VaultActionBatch) -> VaultActionBatch:
    with _vault_advisory_lock(batch.root_fingerprint):
        with transaction.atomic():
            current_batch = VaultActionBatch.objects.select_for_update().get(id=batch.id, user_id=batch.user_id)
            if current_batch.status != VaultActionBatch.Status.APPLYING:
                return current_batch
            root = get_local_kb_root()
            if root is None or current_batch.root_fingerprint != _root_fingerprint(root):
                current_batch.status = VaultActionBatch.Status.MANUAL_REVIEW_REQUIRED
                current_batch.result = {
                    "error": "Configured vault changed or became unavailable during batch recovery."
                }
                current_batch.save(update_fields=["status", "result", "updated_at"])
                return current_batch
            journal = current_batch.rollback_journal
            files = journal.get("files") if isinstance(journal.get("files"), dict) else {}
            if not files:
                current_batch.status = VaultActionBatch.Status.MANUAL_REVIEW_REQUIRED
                current_batch.result = {"error": "Applying batch has no rollback journal."}
                current_batch.save(update_fields=["status", "result", "updated_at"])
                return current_batch

            states: dict[str, tuple[bool, str | None]] = {}
            all_final = True
            safe_to_rollback = True
            unsafe_paths: list[str] = []
            for path, entry in files.items():
                _, target = _safe_target(root, path)
                exists, checksum = _current_hash(target)
                states[path] = (exists, checksum)
                if not exists or checksum != entry.get("final_sha256"):
                    all_final = False
                original_state = (bool(entry.get("exists")), entry.get("original_sha256"))
                final_state = (True, entry.get("final_sha256"))
                if (exists, checksum) not in {original_state, final_state}:
                    safe_to_rollback = False
                    unsafe_paths.append(path)

            if all_final:
                current_batch.status = VaultActionBatch.Status.APPLIED
                current_batch.result = {
                    "files": sorted(files),
                    "recovery": "confirmed_applied",
                    "recovery_artifacts": _find_recovery_artifacts(root, journal),
                }
                current_batch.rollback_journal = {}
            elif safe_to_rollback:
                rollback_errors = _rollback_files(root, journal)
                current_batch.status = (
                    VaultActionBatch.Status.MANUAL_REVIEW_REQUIRED
                    if rollback_errors
                    else VaultActionBatch.Status.FAILED
                )
                current_batch.result = {
                    "files": sorted(files),
                    "recovery": "manual_review_required" if rollback_errors else "rolled_back",
                    "rollback_errors": rollback_errors,
                    "recovery_artifacts": _find_recovery_artifacts(root, journal),
                }
                if not rollback_errors:
                    current_batch.rollback_journal = {}
            else:
                current_batch.status = VaultActionBatch.Status.MANUAL_REVIEW_REQUIRED
                current_batch.result = {
                    "error": "Files no longer match either the original or proposed batch state.",
                    "files": sorted(unsafe_paths),
                    "observed": {path: {"exists": state[0], "sha256": state[1]} for path, state in states.items()},
                    "recovery_artifacts": _find_recovery_artifacts(root, journal),
                }

            current_batch.save(update_fields=["status", "result", "rollback_journal", "updated_at"])
            return current_batch


def recover_incomplete_vault_action_batches() -> None:
    batches = list(
        VaultActionBatch.objects.filter(status=VaultActionBatch.Status.APPLYING)
        .select_related("user")
        .order_by("created_at")
    )
    for batch in batches:
        try:
            recover_vault_action_batch(batch)
        except Exception:
            logger.exception("Failed to recover vault action batch %s", batch.id)


def serialize_vault_action_batch(batch: VaultActionBatch) -> dict[str, Any]:
    return {
        "id": str(batch.id),
        "conversation_id": str(batch.conversation_id),
        "turn_id": str(batch.turn_id),
        "status": batch.status,
        "actions": batch.actions,
        "previews": batch.previews,
        "expires_at": batch.expires_at.isoformat(),
        "result": batch.result,
    }
