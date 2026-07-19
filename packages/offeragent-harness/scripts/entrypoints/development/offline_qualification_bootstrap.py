"""Fail-closed qualification audit guard loaded before OfferAgent package imports."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sys
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

_TRACE_ENV = "OFFERAGENT_OFFLINE_QUALIFICATION_TRACE"
_TOKEN_ENV = "OFFERAGENT_OFFLINE_QUALIFICATION_TOKEN"
_TOKEN = re.compile(r"[0-9a-f]{64}")
_NETWORK_EVENTS = {
    "socket.bind",
    "socket.connect",
    "socket.getaddrinfo",
    "socket.sendmsg",
    "socket.sendto",
}
_PROCESS_EVENTS = {"os.posix_spawn", "os.spawn", "os.system", "subprocess.Popen"}
_installed = False


def install_offline_qualification_guard() -> bool:
    """Install the restrictive audit hook only for an explicitly sealed qualification run."""

    global _installed
    trace_value = os.environ.get(_TRACE_ENV)
    token = os.environ.get(_TOKEN_ENV)
    if trace_value is None and token is None:
        return False
    if _installed:
        return True
    if trace_value is None or token is None or _TOKEN.fullmatch(token) is None:
        raise PermissionError("offline qualification guard identity is invalid")
    trace = Path(trace_value)
    if not trace.is_absolute() or not trace.parent.is_dir():
        raise PermissionError("offline qualification trace location is invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(trace, flags, 0o600)
    token_sha256 = f"sha256:{hashlib.sha256(token.encode()).hexdigest()}"
    sequence = 0

    def record(
        event: str,
        decision: str,
        target: str,
        *,
        system_python: bool = False,
        pip: bool = False,
        download: bool = False,
    ) -> None:
        nonlocal sequence
        value = {
            "decision": decision,
            "event": event,
            "pipInvoked": pip,
            "schemaVersion": 1,
            "sequence": sequence,
            "startupDownloadAttempted": download,
            "systemPythonInvoked": system_python,
            "target": target,
            "tokenSha256": token_sha256,
        }
        sequence += 1
        payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            os.write(descriptor, payload)
        except OSError as error:
            raise PermissionError("offline qualification audit trace failed") from error

    record("guard.installed", "allow", "qualification")

    def audit(event: str, arguments: tuple[Any, ...]) -> None:
        if event in _NETWORK_EVENTS:
            target = _socket_target(_network_address(event, arguments))
            allowed = target in {"loopback", "local"} and _called_from_socketpair()
            if allowed:
                target = "event-loop-socketpair"
            record(
                event,
                "allow" if allowed else "deny",
                target,
                download=not allowed and event != "socket.bind",
            )
            if not allowed:
                raise PermissionError("offline qualification denied an unexpected network attempt")
        elif event in _PROCESS_EVENTS:
            system_python, pip = _process_flags(arguments)
            record(event, "deny", "child-process", system_python=system_python, pip=pip)
            raise PermissionError("offline qualification denied a child process attempt")

    sys.addaudithook(audit)
    _installed = True
    return True


def _network_address(event: str, arguments: tuple[Any, ...]) -> object:
    if event == "socket.getaddrinfo":
        return arguments[0] if arguments else None
    if event == "socket.sendmsg":
        return arguments[4] if len(arguments) > 4 else None
    return arguments[1] if len(arguments) > 1 else None


def _socket_target(value: object) -> str:
    if value is None:
        return "local"
    host: object = value[0] if isinstance(value, tuple) and value else value
    if not isinstance(host, str):
        return "external-or-name"
    try:
        return "loopback" if ipaddress.ip_address(host).is_loopback else "external-or-name"
    except ValueError:
        return "external-or-name"


def _called_from_socketpair() -> bool:
    frame = sys._getframe()
    while True:
        if (
            frame.f_code.co_name in {"socketpair", "_fallback_socketpair"}
            and frame.f_globals.get("__name__") == "socket"
        ):
            return True
        parent = frame.f_back
        if parent is None:
            return False
        frame = parent


def _process_flags(arguments: Sequence[object]) -> tuple[bool, bool]:
    tokens: list[str] = []
    for argument in arguments[:2]:
        if isinstance(argument, str):
            tokens.append(argument)
        elif isinstance(argument, Sequence) and not isinstance(argument, (str, bytes, bytearray)):
            tokens.extend(item for item in argument if isinstance(item, str))
    basenames = [Path(token).name.casefold() for token in tokens]
    system_python = any(
        name in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"} for name in basenames
    )
    pip = any(name in {"pip", "pip.exe", "pip3", "pip3.exe"} for name in basenames) or any(
        left == "-m" and right.casefold() == "pip" for left, right in pairwise(tokens)
    )
    return system_python, pip


__all__ = ["install_offline_qualification_guard"]
