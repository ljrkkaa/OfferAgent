"""AppContainer identity and exact filesystem-ACE lifecycle for child processes.

Network-denied children use one stable Package SID per Workspace.  Filesystem
authority is leased from signed process-profile declarations, journaled before
the DACL mutation, and removed by deleting only the exact ACE this module
added.  Whole security descriptors are never restored, so unrelated ACL edits
made while a child is running are preserved.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import stat
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .process_identity import SupervisedWorkspaceIdentity
from .process_supervisor import ProcessFilesystemAccess, ResolvedProcessFilesystemGrant
from .windows_process import WindowsProcessError

_APP_CONTAINER_PREFIX = "OfferAgent.NoNetwork."
_HRESULT_ALREADY_EXISTS = 0x800700B7
_S_OK = 0
_SDDL_REVISION_1 = 1
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_ACL_REVISION = 2
_MAXDWORD = 0xFFFFFFFF
_ACCESS_ALLOWED_ACE_TYPE = 0
_ACCESS_DENIED_ACE_TYPE = 1
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_INHERIT_ONLY_ACE = 0x08
_INHERITED_ACE = 0x10
_ACE_FLAGS = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE
_FILE_GENERIC_READ = 0x00120089
_FILE_GENERIC_WRITE = 0x00120116
_FILE_GENERIC_EXECUTE = 0x001200A0
_FILE_ALL_ACCESS = 0x001F01FF
_DELETE = 0x00010000
_READ_MASK = _FILE_GENERIC_READ
_READ_WRITE_MASK = _FILE_GENERIC_READ | _FILE_GENERIC_WRITE | _DELETE
_READ_EXECUTE_MASK = _FILE_GENERIC_READ | _FILE_GENERIC_EXECUTE
_ALL_APPLICATION_PACKAGES = "S-1-15-2-1"
_ALL_RESTRICTED_APPLICATION_PACKAGES = "S-1-15-2-2"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_WORKSPACE_INSTANCE_ID = re.compile(r"^wsi_[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class _Acl(ctypes.Structure):
    _fields_ = [
        ("revision", ctypes.c_ubyte),
        ("sbz1", ctypes.c_ubyte),
        ("size", wintypes.WORD),
        ("ace_count", wintypes.WORD),
        ("sbz2", wintypes.WORD),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("ace_type", ctypes.c_ubyte),
        ("ace_flags", ctypes.c_ubyte),
        ("ace_size", wintypes.WORD),
    ]


class _AccessAllowedAce(ctypes.Structure):
    _fields_ = [
        ("header", _AceHeader),
        ("mask", wintypes.DWORD),
        ("sid_start", wintypes.DWORD),
    ]


class _GenericMapping(ctypes.Structure):
    _fields_ = [
        ("generic_read", wintypes.DWORD),
        ("generic_write", wintypes.DWORD),
        ("generic_execute", wintypes.DWORD),
        ("generic_all", wintypes.DWORD),
    ]


class WindowsAppContainerProfile:
    """Stable current-user AppContainer profile for one Workspace."""

    def __init__(self, workspace: SupervisedWorkspaceIdentity) -> None:
        _require_windows()
        self.workspace = workspace
        self.moniker = _APP_CONTAINER_PREFIX + workspace.workspace_instance_id
        if len(self.moniker) > 64:
            raise WindowsProcessError("AppContainer moniker exceeds the Windows profile limit")
        self._lock = threading.Lock()
        self._sid = 0
        self._sid_string: str | None = None
        self._local_app_data: Path | None = None
        self._userenv = ctypes.WinDLL("userenv", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ole32 = ctypes.OleDLL("ole32", use_last_error=True)
        self._configure_api()

    @property
    def sid(self) -> int:
        self.ensure()
        return self._sid

    @property
    def sid_string(self) -> str:
        self.ensure()
        assert self._sid_string is not None
        return self._sid_string

    @property
    def local_app_data(self) -> Path:
        self.ensure()
        assert self._local_app_data is not None
        return self._local_app_data

    def ensure(self) -> None:
        with self._lock:
            if self._sid:
                return
            sid = ctypes.c_void_p()
            result = int(
                self._userenv.CreateAppContainerProfile(
                    self.moniker,
                    "OfferAgent network-denied process",
                    "Workspace-scoped AppContainer for supervised no-network child processes",
                    None,
                    0,
                    ctypes.byref(sid),
                )
            )
            if _unsigned_hresult(result) == _HRESULT_ALREADY_EXISTS:
                result = int(
                    self._userenv.DeriveAppContainerSidFromAppContainerName(
                        self.moniker,
                        ctypes.byref(sid),
                    )
                )
            if result < 0:
                raise _hresult_error("Create/derive AppContainer profile", result)
            raw = int(sid.value or 0)
            if not raw or not self._advapi32.IsValidSid(ctypes.c_void_p(raw)):
                if raw:
                    self._advapi32.FreeSid(ctypes.c_void_p(raw))
                raise WindowsProcessError("Windows returned an invalid AppContainer Package SID")
            text = wintypes.LPWSTR()
            if not self._advapi32.ConvertSidToStringSidW(ctypes.c_void_p(raw), ctypes.byref(text)):
                self._advapi32.FreeSid(ctypes.c_void_p(raw))
                raise _last_error("ConvertSidToStringSidW(AppContainer)")
            try:
                value = text.value
                if not value:
                    raise WindowsProcessError("AppContainer Package SID converted to empty text")
                local_text = wintypes.LPWSTR()
                folder_result = int(self._userenv.GetAppContainerFolderPath(value, ctypes.byref(local_text)))
                if folder_result < 0:
                    raise _hresult_error("GetAppContainerFolderPath", folder_result)
                try:
                    folder_value = local_text.value
                    if not folder_value:
                        raise WindowsProcessError("AppContainer profile folder is unavailable")
                    local_app_data = Path(folder_value).resolve(strict=True)
                    temp = local_app_data / "Temp"
                    temp.mkdir(exist_ok=True)
                    if not temp.is_dir():
                        raise WindowsProcessError("AppContainer Temp directory is unavailable")
                finally:
                    self._ole32.CoTaskMemFree(ctypes.cast(local_text, ctypes.c_void_p))
                self._sid = raw
                self._sid_string = value
                self._local_app_data = local_app_data
            finally:
                self._kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
                if self._sid != raw:
                    self._advapi32.FreeSid(ctypes.c_void_p(raw))

    def close(self) -> None:
        with self._lock:
            sid, self._sid = self._sid, 0
            self._sid_string = None
            self._local_app_data = None
        if sid:
            self._advapi32.FreeSid(ctypes.c_void_p(sid))

    def delete(self) -> None:
        self.close()
        result = int(self._userenv.DeleteAppContainerProfile(self.moniker))
        if result < 0:
            raise _hresult_error("DeleteAppContainerProfile", result)

    def _configure_api(self) -> None:
        self._userenv.CreateAppContainerProfile.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._userenv.CreateAppContainerProfile.restype = ctypes.c_long
        self._userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
        self._userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
        self._userenv.DeleteAppContainerProfile.restype = ctypes.c_long
        self._userenv.GetAppContainerFolderPath.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPWSTR)]
        self._userenv.GetAppContainerFolderPath.restype = ctypes.c_long
        self._advapi32.IsValidSid.argtypes = [ctypes.c_void_p]
        self._advapi32.IsValidSid.restype = wintypes.BOOL
        self._advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        self._advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        self._advapi32.FreeSid.argtypes = [ctypes.c_void_p]
        self._advapi32.FreeSid.restype = ctypes.c_void_p
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p
        self._ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self._ole32.CoTaskMemFree.restype = None


class AppContainerAclLease:
    def __init__(self, manager: WindowsAppContainerAclManager, keys: tuple[tuple[str, int, int], ...]) -> None:
        self._manager = manager
        self._keys = keys
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._manager._release(self._keys)


class WindowsAppContainerAclManager:
    """Reference-count exact Package-SID ACEs with crash recovery."""

    def __init__(self, profile: WindowsAppContainerProfile, state_directory: Path) -> None:
        _require_windows()
        self._profile = profile
        self._state_directory = state_directory.expanduser().resolve(strict=False)
        self._state_directory.mkdir(parents=True, exist_ok=True)
        if not self._state_directory.is_dir() or _is_reparse_point(self._state_directory):
            raise WindowsProcessError("AppContainer ACL state directory is invalid")
        self._journal = self._state_directory / "appcontainer-acl-journal.json"
        self._lock = threading.RLock()
        self._references: dict[tuple[str, int, int], dict[str, Any]] = {}
        self._entries: dict[str, dict[str, Any]] = {}
        self._recovered = False
        if self._journal.exists():
            self.recover()

    @property
    def journal_path(self) -> Path:
        return self._journal

    def acquire(self, grants: tuple[ResolvedProcessFilesystemGrant, ...]) -> AppContainerAclLease:
        with self._lock:
            self.recover()
            sid = self._profile.sid
            sid_string = self._profile.sid_string
            keys: list[tuple[str, int, int]] = []
            try:
                for grant in grants:
                    path = grant.path.resolve(strict=True)
                    identity = _verified_directory_identity(path)
                    mask = _mask_for(grant.access)
                    key = (os.path.normcase(str(path)), mask, _ACE_FLAGS)
                    current = self._references.get(key)
                    if current is not None:
                        if (current["file_device"], current["file_index"]) != identity:
                            raise WindowsProcessError("AppContainer filesystem grant directory identity changed")
                        current["count"] += 1
                        keys.append(key)
                        continue
                    owned, entry_id = self._ensure_ace(
                        path,
                        sid,
                        sid_string,
                        mask,
                        _ACE_FLAGS,
                        identity,
                    )
                    self._references[key] = {
                        "count": 1,
                        "path": path,
                        "owned": owned,
                        "entry_id": entry_id,
                        "file_device": identity[0],
                        "file_index": identity[1],
                    }
                    keys.append(key)
                return AppContainerAclLease(self, tuple(keys))
            except BaseException:
                self._release(tuple(reversed(keys)))
                raise

    def recover(self) -> None:
        with self._lock:
            if self._recovered:
                return
            if not self._journal.exists():
                self._recovered = True
                return
            payload = _load_journal(self._journal)
            if payload["workspaceInstanceId"] != self._profile.workspace.workspace_instance_id:
                raise WindowsProcessError("AppContainer ACL journal belongs to another Workspace")
            if payload["appContainerMoniker"] != self._profile.moniker:
                raise WindowsProcessError("AppContainer ACL journal moniker mismatch")
            entries = payload["entries"]
            if not isinstance(entries, list):
                raise WindowsProcessError("AppContainer ACL journal entries are invalid")
            self._entries = {}
            for raw in entries:
                entry = _validate_entry(raw)
                self._entries[entry["entryId"]] = entry
            if self._entries:
                sid = self._profile.sid
                sid_string = self._profile.sid_string
                for entry_id, entry in tuple(self._entries.items()):
                    if entry["sid"] != sid_string:
                        raise WindowsProcessError("AppContainer ACL journal Package SID mismatch")
                    path = Path(entry["path"])
                    _remove_journaled_ace(path, sid, entry)
                    self._entries.pop(entry_id, None)
                    self._persist()
            self._recovered = True

    def shutdown(self) -> None:
        with self._lock:
            if self._references:
                raise WindowsProcessError("AppContainer ACL manager stopped with active leases")
            self.recover()

    def _ensure_ace(
        self,
        path: Path,
        sid: int,
        sid_string: str,
        mask: int,
        ace_flags: int,
        identity: tuple[int, int],
    ) -> tuple[bool, str | None]:
        if _appcontainer_principal_has_access(path, sid, mask):
            return False, None
        entry_id = uuid.uuid4().hex
        entry = {
            "entryId": entry_id,
            "path": str(path),
            "sid": sid_string,
            "mask": mask,
            "aceFlags": ace_flags,
            "fileDevice": str(identity[0]),
            "fileIndex": str(identity[1]),
            "state": "pending_add",
            "originalDaclSha256": _dacl_sha256(path),
            "addedDaclSha256": None,
        }
        self._entries[entry_id] = entry
        self._persist()
        added = _add_exact_ace(path, sid, mask, ace_flags)
        if not added:
            self._entries.pop(entry_id, None)
            self._persist()
            return False, None
        entry["state"] = "active"
        entry["addedDaclSha256"] = _dacl_sha256(path)
        self._persist()
        return True, entry_id

    def _release(self, keys: tuple[tuple[str, int, int], ...]) -> None:
        with self._lock:
            sid = self._profile.sid if keys else 0
            for key in keys:
                current = self._references.get(key)
                if current is None:
                    continue
                current["count"] -= 1
                if current["count"] > 0:
                    continue
                self._references.pop(key, None)
                if not current["owned"]:
                    continue
                entry_id = current["entry_id"]
                entry = self._entries.get(entry_id)
                if entry is None:
                    raise WindowsProcessError("owned AppContainer ACE is missing its recovery journal")
                entry["state"] = "pending_remove"
                self._persist()
                path = current["path"]
                _remove_journaled_ace(path, sid, entry)
                self._entries.pop(entry_id, None)
                self._persist()

    def _persist(self) -> None:
        if not self._entries:
            try:
                self._journal.unlink()
            except FileNotFoundError:
                pass
            return
        payload = {
            "schemaVersion": 2,
            "workspaceInstanceId": self._profile.workspace.workspace_instance_id,
            "appContainerMoniker": self._profile.moniker,
            "entries": [self._entries[key] for key in sorted(self._entries)],
        }
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary = self._journal.with_name(f".{self._journal.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._journal)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def cleanup_workspace_appcontainer(
    workspace: SupervisedWorkspaceIdentity,
    state_directory: Path,
) -> None:
    """Uninstall/unregister cleanup: recover exact owned ACEs, then delete profile."""

    profile = WindowsAppContainerProfile(workspace)
    try:
        manager = WindowsAppContainerAclManager(profile, state_directory)
        manager.shutdown()
        profile.delete()
    finally:
        profile.close()


def filesystem_dacl_sddl(path: Path) -> str:
    _require_windows()
    advapi32, kernel32 = _acl_apis()
    with _dacl(path, advapi32, kernel32) as (_, descriptor):
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            _SDDL_REVISION_1,
            _DACL_SECURITY_INFORMATION,
            ctypes.byref(text),
            None,
        ):
            raise _last_error("ConvertSecurityDescriptorToStringSecurityDescriptorW(filesystem)")
        try:
            value = text.value
            if value is None:
                raise WindowsProcessError("filesystem DACL converted to empty SDDL")
            return value
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _mask_for(access: ProcessFilesystemAccess) -> int:
    return {
        ProcessFilesystemAccess.READ: _READ_MASK,
        ProcessFilesystemAccess.READ_WRITE: _READ_WRITE_MASK,
        ProcessFilesystemAccess.READ_EXECUTE: _READ_EXECUTE_MASK,
    }[access]


def _appcontainer_principal_has_access(path: Path, package_sid: int, desired: int) -> bool:
    advapi32, kernel32 = _acl_apis()
    allocated: list[int] = []
    try:
        candidates = [package_sid]
        for value in (_ALL_APPLICATION_PACKAGES, _ALL_RESTRICTED_APPLICATION_PACKAGES):
            candidate_sid = ctypes.c_void_p()
            if not advapi32.ConvertStringSidToSidW(value, ctypes.byref(candidate_sid)):
                raise _last_error("ConvertStringSidToSidW(AppContainer group)")
            raw = int(candidate_sid.value or 0)
            allocated.append(raw)
            candidates.append(raw)
        with _dacl(path, advapi32, kernel32) as (dacl, _):
            if not dacl:
                return True
            remaining = desired
            acl = ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents
            for index in range(int(acl.ace_count)):
                ace = _get_ace(advapi32, dacl, index)
                header = ctypes.cast(ace, ctypes.POINTER(_AceHeader)).contents
                if header.ace_flags & _INHERIT_ONLY_ACE:
                    continue
                if header.ace_type not in {_ACCESS_ALLOWED_ACE_TYPE, _ACCESS_DENIED_ACE_TYPE}:
                    continue
                allowed = ctypes.cast(ace, ctypes.POINTER(_AccessAllowedAce)).contents
                ace_address = ace.value
                if ace_address is None:
                    raise WindowsProcessError("filesystem DACL returned a null ACE")
                ace_sid = ctypes.c_void_p(ace_address + _AccessAllowedAce.sid_start.offset)
                if not any(advapi32.EqualSid(ace_sid, ctypes.c_void_p(item)) for item in candidates):
                    continue
                mask = _map_file_mask(advapi32, int(allowed.mask))
                if header.ace_type == _ACCESS_DENIED_ACE_TYPE and mask & remaining:
                    return False
                if header.ace_type == _ACCESS_ALLOWED_ACE_TYPE:
                    remaining &= ~mask
                    if remaining == 0:
                        return True
            return remaining == 0
    finally:
        for allocated_sid in allocated:
            kernel32.LocalFree(ctypes.c_void_p(allocated_sid))


def _add_exact_ace(path: Path, sid: int, mask: int, ace_flags: int) -> bool:
    advapi32, kernel32 = _acl_apis()
    with _dacl(path, advapi32, kernel32) as (dacl, _):
        if dacl and _find_exact_ace(advapi32, dacl, sid, mask, ace_flags) is not None:
            return False
        sid_length = int(advapi32.GetLengthSid(ctypes.c_void_p(sid)))
        if sid_length <= 0:
            raise _last_error("GetLengthSid(AppContainer)")
        ace_length = ctypes.sizeof(_AccessAllowedAce) - ctypes.sizeof(wintypes.DWORD) + sid_length
        old_size = ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents.size if dacl else ctypes.sizeof(_Acl)
        buffer = ctypes.create_string_buffer(int(old_size) + ace_length)
        new_acl = ctypes.cast(buffer, ctypes.c_void_p)
        if not advapi32.InitializeAcl(new_acl, len(buffer), _ACL_REVISION):
            raise _last_error("InitializeAcl(add AppContainer ACE)")
        inserted = False
        if dacl:
            old = ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents
            for index in range(int(old.ace_count)):
                ace = _get_ace(advapi32, dacl, index)
                header = ctypes.cast(ace, ctypes.POINTER(_AceHeader)).contents
                if not inserted and header.ace_flags & _INHERITED_ACE:
                    _append_allowed_ace(advapi32, new_acl, ace_flags, mask, sid)
                    inserted = True
                if not advapi32.AddAce(new_acl, _ACL_REVISION, _MAXDWORD, ace, header.ace_size):
                    raise _last_error("AddAce(copy existing DACL)")
        if not inserted:
            _append_allowed_ace(advapi32, new_acl, ace_flags, mask, sid)
        _set_dacl(advapi32, path, new_acl)
        return True


def _remove_one_exact_ace(path: Path, sid: int, mask: int, ace_flags: int) -> bool:
    advapi32, kernel32 = _acl_apis()
    with _dacl(path, advapi32, kernel32) as (dacl, _):
        if not dacl:
            return False
        remove_index = _find_exact_ace(advapi32, dacl, sid, mask, ace_flags)
        if remove_index is None:
            return False
        old = ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents
        buffer = ctypes.create_string_buffer(int(old.size))
        new_acl = ctypes.cast(buffer, ctypes.c_void_p)
        if not advapi32.InitializeAcl(new_acl, len(buffer), _ACL_REVISION):
            raise _last_error("InitializeAcl(remove AppContainer ACE)")
        for index in range(int(old.ace_count)):
            if index == remove_index:
                continue
            ace = _get_ace(advapi32, dacl, index)
            header = ctypes.cast(ace, ctypes.POINTER(_AceHeader)).contents
            if not advapi32.AddAce(new_acl, _ACL_REVISION, _MAXDWORD, ace, header.ace_size):
                raise _last_error("AddAce(copy DACL during cleanup)")
        _set_dacl(advapi32, path, new_acl)
        return True


def _find_exact_ace(advapi32: Any, dacl: ctypes.c_void_p, sid: int, mask: int, ace_flags: int) -> int | None:
    acl = ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents
    for index in range(int(acl.ace_count)):
        ace = _get_ace(advapi32, dacl, index)
        allowed = ctypes.cast(ace, ctypes.POINTER(_AccessAllowedAce)).contents
        if (
            allowed.header.ace_type == _ACCESS_ALLOWED_ACE_TYPE
            and allowed.header.ace_flags == ace_flags
            and int(allowed.mask) == mask
        ):
            ace_address = ace.value
            if ace_address is None:
                raise WindowsProcessError("filesystem DACL returned a null ACE")
            ace_sid = ctypes.c_void_p(ace_address + _AccessAllowedAce.sid_start.offset)
            if advapi32.EqualSid(ace_sid, ctypes.c_void_p(sid)):
                return index
    return None


def _append_allowed_ace(advapi32: Any, acl: ctypes.c_void_p, ace_flags: int, mask: int, sid: int) -> None:
    if not advapi32.AddAccessAllowedAceEx(
        acl,
        _ACL_REVISION,
        ace_flags,
        mask,
        ctypes.c_void_p(sid),
    ):
        raise _last_error("AddAccessAllowedAceEx(AppContainer)")


def _map_file_mask(advapi32: Any, mask: int) -> int:
    value = wintypes.DWORD(mask)
    mapping = _GenericMapping(
        _FILE_GENERIC_READ,
        _FILE_GENERIC_WRITE,
        _FILE_GENERIC_EXECUTE,
        _FILE_ALL_ACCESS,
    )
    advapi32.MapGenericMask(ctypes.byref(value), ctypes.byref(mapping))
    return int(value.value)


@contextmanager
def _dacl(path: Path, advapi32: Any, kernel32: Any) -> Iterator[tuple[ctypes.c_void_p, ctypes.c_void_p]]:
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = int(
        advapi32.GetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
    )
    if status:
        raise WindowsProcessError(status, f"GetNamedSecurityInfoW failed: {ctypes.FormatError(status)}")
    try:
        yield dacl, descriptor
    finally:
        kernel32.LocalFree(descriptor)


def _get_ace(advapi32: Any, dacl: ctypes.c_void_p, index: int) -> ctypes.c_void_p:
    ace = ctypes.c_void_p()
    if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
        raise _last_error("GetAce(filesystem DACL)")
    return ace


def _set_dacl(advapi32: Any, path: Path, dacl: ctypes.c_void_p) -> None:
    status = int(
        advapi32.SetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
    )
    if status:
        raise WindowsProcessError(status, f"SetNamedSecurityInfoW failed: {ctypes.FormatError(status)}")


def _acl_apis() -> tuple[Any, Any]:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.InitializeAcl.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    advapi32.InitializeAcl.restype = wintypes.BOOL
    advapi32.AddAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    advapi32.AddAce.restype = wintypes.BOOL
    advapi32.AddAccessAllowedAceEx.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    advapi32.AddAccessAllowedAceEx.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.MapGenericMask.argtypes = [ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_GenericMapping)]
    advapi32.MapGenericMask.restype = None
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return advapi32, kernel32


def _dacl_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(filesystem_dacl_sddl(path).encode("utf-8")).hexdigest()


def _load_journal(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WindowsProcessError("AppContainer ACL recovery journal is unreadable") from error
    if not isinstance(value, dict) or set(value) != {
        "schemaVersion",
        "workspaceInstanceId",
        "appContainerMoniker",
        "entries",
    }:
        raise WindowsProcessError("AppContainer ACL recovery journal is invalid")
    if value["schemaVersion"] != 2:
        raise WindowsProcessError("AppContainer ACL recovery journal version is unsupported")
    return value


def _validate_entry(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {
        "entryId",
        "path",
        "sid",
        "mask",
        "aceFlags",
        "fileDevice",
        "fileIndex",
        "state",
        "originalDaclSha256",
        "addedDaclSha256",
    }:
        raise WindowsProcessError("AppContainer ACL recovery entry is invalid")
    if (
        not isinstance(raw["entryId"], str)
        or not isinstance(raw["path"], str)
        or not Path(raw["path"]).is_absolute()
        or not isinstance(raw["sid"], str)
        or not isinstance(raw["mask"], int)
        or not isinstance(raw["aceFlags"], int)
        or not isinstance(raw["fileDevice"], str)
        or re.fullmatch(r"(?:0|[1-9][0-9]{0,31})", raw["fileDevice"]) is None
        or not isinstance(raw["fileIndex"], str)
        or re.fullmatch(r"[1-9][0-9]{0,31}", raw["fileIndex"]) is None
        or raw["state"] not in {"pending_add", "active", "pending_remove"}
        or not isinstance(raw["originalDaclSha256"], str)
        or (raw["addedDaclSha256"] is not None and not isinstance(raw["addedDaclSha256"], str))
    ):
        raise WindowsProcessError("AppContainer ACL recovery entry fields are invalid")
    return dict(raw)


def _verified_directory_identity(path: Path) -> tuple[int, int]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or bool(int(getattr(info, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT)
        or info.st_ino <= 0
    ):
        raise WindowsProcessError("AppContainer filesystem grant must remain a real directory")
    return int(info.st_dev), int(info.st_ino)


def _remove_journaled_ace(path: Path, sid: int, entry: dict[str, Any]) -> None:
    try:
        identity = _verified_directory_identity(path)
    except FileNotFoundError:
        return
    expected = (int(entry["fileDevice"]), int(entry["fileIndex"]))
    if identity != expected:
        raise WindowsProcessError("AppContainer ACL target directory identity changed before cleanup")
    _remove_one_exact_ace(path, sid, entry["mask"], entry["aceFlags"])


def _is_reparse_point(path: Path) -> bool:
    stat = path.lstat()
    return path.is_symlink() or bool(int(getattr(stat, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _unsigned_hresult(value: int) -> int:
    return value & 0xFFFFFFFF


def _hresult_error(operation: str, value: int) -> WindowsProcessError:
    unsigned = _unsigned_hresult(value)
    code = unsigned & 0xFFFF if unsigned & 0xFFFF0000 == 0x80070000 else unsigned
    return WindowsProcessError(code, f"{operation} failed (HRESULT 0x{unsigned:08x}): {ctypes.FormatError(code)}")


def _last_error(operation: str) -> WindowsProcessError:
    code = ctypes.get_last_error()
    return WindowsProcessError(code, f"{operation} failed: {ctypes.FormatError(code)}")


def _require_windows() -> None:
    if os.name != "nt":
        raise WindowsProcessError("AppContainer isolation is only available on Windows")


__all__ = [
    "AppContainerAclLease",
    "WindowsAppContainerAclManager",
    "WindowsAppContainerProfile",
    "cleanup_workspace_appcontainer",
    "filesystem_dacl_sddl",
]
