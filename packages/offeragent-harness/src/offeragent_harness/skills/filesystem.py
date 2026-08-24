"""No-follow, handle-verified filesystem access for explicit Skill roots."""

from __future__ import annotations

import asyncio
import os
import re
import stat
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TypeVar

from offeragent_harness.ports.cancellation import CancellationToken, OperationCancelled

from .models import SkillError, SkillErrorCode, SkillFileFact, SkillLayer, SkillLimits, SkillRoot

T = TypeVar("T")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_READ_BLOCK = 64 * 1024
_HEADER_BLOCK = 256
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
_RESERVED = frozenset(
    {
        "CON",
        "CONIN$",
        "CONOUT$",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{suffix}" for suffix in "123456789¹²³"),
        *(f"LPT{suffix}" for suffix in "123456789¹²³"),
    }
)


@dataclass(frozen=True, slots=True)
class DiscoveredSkillFile:
    path: Path
    package_path: str
    fact: SkillFileFact


@dataclass(frozen=True, slots=True)
class SkillHeaderRead:
    content: bytes
    bytes_read: int
    fact: SkillFileFact


@dataclass(frozen=True, slots=True)
class SkillFileRead:
    content: bytes
    fact: SkillFileFact


class SecureSkillRoot:
    """One immutable root capability; no ambient cwd or Vault authority is used."""

    def __init__(self, root: SkillRoot, limits: SkillLimits) -> None:
        self.root = root
        self.limits = limits
        self.path = _validate_root(root)
        info = _lstat(self.path, "")
        if not stat.S_ISDIR(info.st_mode):
            raise SkillError(SkillErrorCode.INVALID_ROOT, "Skill root is not a directory")
        self._root_fact = _fact(info)

    async def discover(self, cancellation: CancellationToken) -> tuple[DiscoveredSkillFile, ...]:
        cancellation.checkpoint()
        return await _run_io(self._discover_sync, cancellation)

    async def read_header(
        self,
        discovered: DiscoveredSkillFile,
        cancellation: CancellationToken,
    ) -> SkillHeaderRead:
        cancellation.checkpoint()
        return await _run_io(self._read_header_sync, discovered, cancellation)

    async def read_skill(
        self,
        relative_path: str,
        expected: SkillFileFact,
        cancellation: CancellationToken,
    ) -> SkillFileRead:
        return await self._read_relative(relative_path, expected, self.limits.max_skill_bytes, cancellation)

    async def verify_skill_fact(
        self,
        relative_path: str,
        expected: SkillFileFact,
        cancellation: CancellationToken,
    ) -> None:
        cancellation.checkpoint()

        def verify() -> None:
            resolved = self._resolve(relative_path)
            if _fact(_lstat(resolved, relative_path)) != expected:
                raise SkillError(SkillErrorCode.HASH_DRIFT, "Skill file facts changed after caching")
            self._validate_root()

        await _run_io(verify)

    async def _read_relative(
        self,
        relative_path: str,
        expected: SkillFileFact,
        limit: int,
        cancellation: CancellationToken,
    ) -> SkillFileRead:
        cancellation.checkpoint()
        return await _run_io(self._read_full_sync, relative_path, expected, limit, cancellation)

    def _discover_sync(self, cancellation: CancellationToken) -> tuple[DiscoveredSkillFile, ...]:
        self._validate_root()
        queue: deque[tuple[Path, str, int]] = deque([(self.path, "", 0)])
        output: list[DiscoveredSkillFile] = []
        scanned = 0
        while queue:
            cancellation.checkpoint()
            directory, relative_directory, depth = queue.popleft()
            before = _lstat(directory, relative_directory)
            if not stat.S_ISDIR(before.st_mode):
                raise SkillError(SkillErrorCode.CHANGED_DURING_READ, "Skill directory changed kind")
            names: set[str] = set()
            children: list[os.DirEntry[str]] = []
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    cancellation.checkpoint()
                    scanned += 1
                    if scanned > self.limits.max_scanned_entries:
                        raise SkillError(SkillErrorCode.LIMIT_EXCEEDED, "Skill root scan exceeds entry limit")
                    folded = entry.name.casefold()
                    if folded in names:
                        raise SkillError(
                            SkillErrorCode.CASEFOLD_COLLISION,
                            f"Skill root entries collide under Windows case folding: {entry.name}",
                        )
                    names.add(folded)
                    children.append(entry)
            for entry in sorted(children, key=lambda item: item.name.casefold()):
                _validate_visible_name(entry.name)
                relative = f"{relative_directory}/{entry.name}" if relative_directory else entry.name
                info = _lstat(Path(entry.path), relative)
                if stat.S_ISDIR(info.st_mode):
                    if depth >= self.limits.max_depth:
                        raise SkillError(
                            SkillErrorCode.LIMIT_EXCEEDED,
                            f"Skill directory exceeds depth {self.limits.max_depth}: {relative}",
                        )
                    queue.append((Path(entry.path), relative, depth + 1))
                elif stat.S_ISREG(info.st_mode) and entry.name == "SKILL.md":
                    if not relative_directory:
                        raise SkillError(SkillErrorCode.PATH_POLICY, "SKILL.md must live inside a package directory")
                    _ensure_regular_single_link(info, relative)
                    if info.st_size > self.limits.max_skill_bytes:
                        raise SkillError(SkillErrorCode.LIMIT_EXCEEDED, "SKILL.md exceeds configured byte limit")
                    output.append(DiscoveredSkillFile(Path(entry.path), relative_directory, _fact(info)))
                    if len(output) > self.limits.max_skills:
                        raise SkillError(SkillErrorCode.LIMIT_EXCEEDED, "Skill count exceeds configured limit")
            after = _lstat(directory, relative_directory)
            if _version(before) != _version(after):
                raise SkillError(SkillErrorCode.CHANGED_DURING_READ, "Skill directory changed during discovery")
        self._validate_root()
        return tuple(output)

    def _read_header_sync(
        self,
        discovered: DiscoveredSkillFile,
        cancellation: CancellationToken,
    ) -> SkillHeaderRead:
        relative = f"{discovered.package_path}/SKILL.md"
        resolved = self._resolve(relative)
        with self._open_verified(resolved, relative, discovered.fact) as stream:
            buffer = bytearray()
            while len(buffer) <= self.limits.max_metadata_bytes:
                cancellation.checkpoint()
                chunk = stream.read(_HEADER_BLOCK)
                cancellation.checkpoint()
                if not chunk:
                    break
                buffer.extend(chunk)
                end = _frontmatter_end(bytes(buffer))
                if end is not None:
                    after = _fact(os.fstat(stream.fileno()))
                    if not _same_open_fact(after, discovered.fact):
                        raise SkillError(SkillErrorCode.CHANGED_DURING_READ, "SKILL.md changed during discovery")
                    self._validate_open_path(resolved, relative, after)
                    self._validate_root()
                    return SkillHeaderRead(bytes(buffer[:end]), len(buffer), discovered.fact)
            if len(buffer) > self.limits.max_metadata_bytes:
                raise SkillError(SkillErrorCode.LIMIT_EXCEEDED, "SKILL.md metadata exceeds configured limit")
            raise SkillError(SkillErrorCode.INVALID_FRONTMATTER, "SKILL.md frontmatter is incomplete")

    def _read_full_sync(
        self,
        relative_path: str,
        expected: SkillFileFact,
        limit: int,
        cancellation: CancellationToken,
    ) -> SkillFileRead:
        resolved = self._resolve(relative_path)
        with self._open_verified(resolved, relative_path, expected) as stream:
            output = bytearray()
            while len(output) <= limit:
                cancellation.checkpoint()
                chunk = stream.read(min(_READ_BLOCK, limit + 1 - len(output)))
                cancellation.checkpoint()
                if not chunk:
                    break
                output.extend(chunk)
            if len(output) > limit:
                raise SkillError(SkillErrorCode.LIMIT_EXCEEDED, f"Skill file exceeds {limit} bytes")
            after = _fact(os.fstat(stream.fileno()))
            if not _same_open_fact(after, expected):
                raise SkillError(SkillErrorCode.CHANGED_DURING_READ, "Skill file changed while being read")
            self._validate_open_path(resolved, relative_path, after)
        self._validate_root()
        return SkillFileRead(bytes(output), expected)

    def _open_verified(self, path: Path, relative_path: str, expected: SkillFileFact) -> BinaryIO:
        self._validate_root()
        before_info = _lstat(path, relative_path)
        _ensure_regular_single_link(before_info, relative_path)
        if _fact(before_info) != expected:
            raise SkillError(SkillErrorCode.HASH_DRIFT, "Skill file facts changed after discovery", path=relative_path)
        descriptor = _open_readonly_fd(path)
        stream: BinaryIO | None = None
        try:
            stream = os.fdopen(descriptor, "rb", buffering=0)
            opened = os.fstat(stream.fileno())
            _ensure_regular_single_link(opened, relative_path)
            if not _same_open_fact(_fact(opened), expected):
                raise SkillError(SkillErrorCode.CHANGED_DURING_READ, "Skill path changed before handle open")
            self._validate_open_path(path, relative_path, expected)
            return stream
        except BaseException:
            if stream is not None:
                stream.close()
            else:
                os.close(descriptor)
            raise

    def _validate_open_path(self, path: Path, relative_path: str, opened: SkillFileFact) -> None:
        current = _lstat(path, relative_path)
        if not _same_open_fact(_fact(current), opened):
            raise SkillError(SkillErrorCode.CHANGED_DURING_READ, "Skill path no longer identifies open handle")
        resolved = path.resolve(strict=True)
        if not _contained(self.path, resolved):
            raise SkillError(SkillErrorCode.PATH_POLICY, "Skill path escaped its root")

    def _resolve(self, relative_path: str) -> Path:
        _validate_visible_relative(relative_path)
        candidate = self.path.joinpath(*relative_path.split("/"))
        _reject_reparse_ancestors(self.path, candidate, relative_path)
        resolved = candidate.resolve(strict=True)
        if not _contained(self.path, resolved):
            raise SkillError(SkillErrorCode.PATH_POLICY, "Skill path escapes its explicit root")
        return resolved

    def _validate_root(self) -> None:
        info = _lstat(self.path, "")
        if not stat.S_ISDIR(info.st_mode) or _fact(info) != self._root_fact:
            raise SkillError(SkillErrorCode.CHANGED_DURING_READ, "Skill root identity changed")


def _validate_root(root: SkillRoot) -> Path:
    raw = root.path.expanduser()
    if not raw.is_absolute():
        raise SkillError(SkillErrorCode.INVALID_ROOT, "Skill roots must be explicit absolute paths")
    _reject_reparse_chain(raw, "")
    try:
        raw_info = os.stat(raw, follow_symlinks=False)
    except OSError as error:
        raise SkillError(SkillErrorCode.INVALID_ROOT, f"Skill root is unavailable: {raw}") from error
    _ensure_not_reparse(raw_info, "")
    canonical = raw.resolve(strict=True)
    if root.layer is SkillLayer.WORKSPACE:
        assert root.workspace_root is not None
        raw_workspace = root.workspace_root.expanduser()
        if not raw_workspace.is_absolute():
            raise SkillError(SkillErrorCode.INVALID_ROOT, "Workspace root must be an explicit absolute path")
        _reject_reparse_chain(raw_workspace, ".")
        workspace = raw_workspace.resolve(strict=True)
        expected = workspace / ".claude" / "skills"
        if os.path.normcase(os.path.abspath(canonical)) != os.path.normcase(os.path.abspath(expected)):
            raise SkillError(
                SkillErrorCode.INVALID_ROOT,
                "Workspace Skills are only authorized at the exact .claude/skills capability",
            )
        _reject_reparse_ancestors(workspace, canonical, ".claude/skills")
    return canonical


def _reject_reparse_chain(path: Path, relative_path: str) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = os.stat(current, follow_symlinks=False)
        except OSError as error:
            raise SkillError(SkillErrorCode.INVALID_ROOT, f"Skill root ancestor is unavailable: {current}") from error
        _ensure_not_reparse(info, relative_path)


def _reject_reparse_ancestors(root: Path, candidate: Path, relative_path: str) -> None:
    try:
        parts = candidate.relative_to(root).parts
    except ValueError as error:
        raise SkillError(SkillErrorCode.PATH_POLICY, "Skill path is outside its root") from error
    current = root
    for part in parts:
        current /= part
        try:
            info = os.stat(current, follow_symlinks=False)
        except FileNotFoundError:
            break
        _ensure_not_reparse(info, relative_path)


def _validate_visible_relative(value: str) -> None:
    if (
        not value
        or value.startswith(("/", "\\"))
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or len(value) > 2_048
    ):
        raise SkillError(SkillErrorCode.PATH_POLICY, "Skill path is not a canonical relative path")
    for part in value.split("/"):
        if part in {"", ".", ".."} or part.startswith("."):
            raise SkillError(SkillErrorCode.PATH_POLICY, "hidden/traversal Skill paths are forbidden")
        _validate_visible_name(part)


def _validate_visible_name(value: str) -> None:
    if not value or value.startswith(".") or value[-1] in {".", " "}:
        raise SkillError(SkillErrorCode.PATH_POLICY, f"invalid visible Skill path segment: {value!r}")
    if any(character in _WINDOWS_FORBIDDEN or ord(character) < 32 for character in value):
        raise SkillError(SkillErrorCode.PATH_POLICY, f"Windows-forbidden Skill path segment: {value!r}")
    if value.split(".", maxsplit=1)[0].upper() in _RESERVED:
        raise SkillError(SkillErrorCode.PATH_POLICY, f"reserved Windows Skill path segment: {value!r}")
    if re.match(r"^[A-Za-z]:", value):
        raise SkillError(SkillErrorCode.PATH_POLICY, "drive syntax is forbidden in Skill paths")


def _frontmatter_end(content: bytes) -> int | None:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].rstrip(b"\r\n") != b"---":
        raise SkillError(SkillErrorCode.INVALID_FRONTMATTER, "SKILL.md must begin with ---")
    position = len(lines[0])
    for line in lines[1:]:
        position += len(line)
        if line.rstrip(b"\r\n") == b"---":
            return position
    return None


def _lstat(path: Path, relative_path: str) -> os.stat_result:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise SkillError(SkillErrorCode.CHANGED_DURING_READ, f"Skill path is unavailable: {relative_path}") from error
    _ensure_not_reparse(info, relative_path)
    return info


def _ensure_not_reparse(info: os.stat_result, relative_path: str) -> None:
    attributes = int(getattr(info, "st_file_attributes", 0))
    if stat.S_ISLNK(info.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise SkillError(SkillErrorCode.REPARSE_POINT, f"Skill path is a symlink/junction: {relative_path}")


def _ensure_regular_single_link(info: os.stat_result, relative_path: str) -> None:
    _ensure_not_reparse(info, relative_path)
    if not stat.S_ISREG(info.st_mode):
        raise SkillError(SkillErrorCode.PATH_POLICY, f"Skill file is not regular: {relative_path}")
    if info.st_nlink > 1:
        raise SkillError(SkillErrorCode.PATH_POLICY, f"hard-linked Skill files are forbidden: {relative_path}")


def _fact(info: os.stat_result) -> SkillFileFact:
    return SkillFileFact(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _same_open_fact(left: SkillFileFact, right: SkillFileFact) -> bool:
    """Compare path/handle facts without Windows' inconsistent creation-time projection."""

    return (
        left.device,
        left.inode,
        left.size,
        left.mtime_ns,
    ) == (
        right.device,
        right.inode,
        right.size,
        right.mtime_ns,
    )


def _version(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _contained(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(root), os.path.normcase(candidate))) == os.path.normcase(root)
    except ValueError:
        return False


def _open_readonly_fd(path: Path) -> int:
    if os.name == "nt":
        import _winapi
        import msvcrt

        handle = _winapi.CreateFile(
            _windows_extended_path(path),
            0x80000000,
            0x00000001,
            0,
            3,
            0x00200000 | 0x08000000,
            0,
        )
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _winapi.CloseHandle(handle)
            raise
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _windows_extended_path(path: Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


async def _run_io(function: Callable[..., T], *args: object) -> T:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except BaseException:
            pass
        raise
    except OperationCancelled:
        raise


__all__ = [
    "DiscoveredSkillFile",
    "SecureSkillRoot",
    "SkillFileRead",
    "SkillHeaderRead",
]
