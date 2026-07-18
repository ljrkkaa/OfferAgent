"""Typed line-protocol client for the sealed Windows product qualification driver."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import IO, Any


class QualificationDriverError(RuntimeError):
    """The sealed adapter violated its protocol or rejected a command."""


class QualificationDriverClient:
    """Own one sealed driver process while keeping product events out of RPC replies."""

    def __init__(
        self,
        *,
        executable: Path,
        driver: Path,
        working_directory: Path,
        source_root_guard: Path,
    ) -> None:
        self._executable = _regular_file(executable, "qualification executable")
        self._driver = _regular_file(driver, "qualification driver")
        self._working_directory = _directory(working_directory, "qualification working directory")
        self._source_root_guard = _directory(source_root_guard, "source root guard")
        environment = os.environ.copy()
        environment["NODE_PATH"] = ""
        environment["OFFERAGENT_QUALIFICATION_FORBID_SOURCE_ROOT"] = str(self._source_root_guard)
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._process = subprocess.Popen(
            [str(self._executable), str(self._driver), "serve"],
            cwd=self._working_directory,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            bufsize=1,
            creationflags=creation_flags,
        )
        if self._process.stdin is None or self._process.stdout is None or self._process.stderr is None:
            raise QualificationDriverError("qualification driver pipes are unavailable")
        self._stdin: IO[str] = self._process.stdin
        self._responses: dict[str, queue.Queue[dict[str, Any] | BaseException]] = {}
        self._events: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._lock = threading.Lock()
        self._next_request_id = 0
        self._stderr: list[str] = []
        self._closed = False
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    @property
    def return_code(self) -> int | None:
        return self._process.poll()

    def request(
        self,
        command: str,
        params: dict[str, Any],
        *,
        timeout: float = 60,
    ) -> dict[str, Any]:
        if self._closed:
            raise QualificationDriverError("qualification driver is closed")
        if not command or "\x00" in command or not isinstance(params, dict):
            raise ValueError("qualification command is invalid")
        with self._lock:
            self._next_request_id += 1
            request_id = f"py_{self._next_request_id}"
            response_queue: queue.Queue[dict[str, Any] | BaseException] = queue.Queue(maxsize=1)
            self._responses[request_id] = response_queue
            payload = _json_line({"id": request_id, "command": command, "params": params})
            try:
                self._stdin.write(payload)
                self._stdin.flush()
            except (OSError, UnicodeError) as error:
                self._responses.pop(request_id, None)
                raise QualificationDriverError("qualification driver stdin failed") from error
        try:
            try:
                response = response_queue.get(timeout=timeout)
            except queue.Empty as error:
                raise QualificationDriverError(f"qualification command timed out: {command}") from error
        finally:
            self._responses.pop(request_id, None)
        if isinstance(response, BaseException):
            raise QualificationDriverError(str(response)) from response
        if response.get("ok") is not True:
            message = response.get("error")
            if not isinstance(message, str) or not message:
                message = "qualification driver returned an invalid failure"
            raise QualificationDriverError(message)
        result = response.get("result")
        if not isinstance(result, dict):
            raise QualificationDriverError("qualification driver result is not an object")
        return result

    def next_event(self, *, timeout: float) -> dict[str, Any]:
        try:
            event = self._events.get(timeout=timeout)
        except queue.Empty as error:
            raise QualificationDriverError("qualification product event timed out") from error
        if isinstance(event, BaseException):
            raise QualificationDriverError(str(event)) from event
        return event

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._process.poll() is None:
                try:
                    self.request("stop", {}, timeout=45)
                except QualificationDriverError:
                    pass
                try:
                    self._process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait(timeout=5)
        finally:
            self._closed = True
            try:
                self._stdin.close()
            except OSError:
                pass
            self._reader.join(timeout=5)
            self._stderr_reader.join(timeout=5)
        if self._process.returncode != 0:
            details = "".join(self._stderr)[-8_192:].strip()
            raise QualificationDriverError(
                f"qualification driver exited {self._process.returncode}: {details}"
            )

    def __enter__(self) -> QualificationDriverClient:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _read_stdout(self) -> None:
        try:
            assert self._process.stdout is not None
            for raw in self._process.stdout:
                value = _object(raw, "qualification driver output")
                if "id" in value:
                    request_id = value.get("id")
                    if not isinstance(request_id, str):
                        raise QualificationDriverError("qualification driver response ID is invalid")
                    target = self._responses.get(request_id)
                    if target is None:
                        raise QualificationDriverError("qualification driver returned an unknown response ID")
                    target.put(value)
                    continue
                event_name = value.get("event")
                if not isinstance(event_name, str) or not event_name:
                    raise QualificationDriverError("qualification driver event is invalid")
                self._events.put(value)
            if not self._closed and self._process.poll() not in (0, None):
                raise QualificationDriverError("qualification driver output closed unexpectedly")
        except BaseException as error:  # propagate reader failures to every waiter
            self._events.put(error)
            for target in tuple(self._responses.values()):
                try:
                    target.put_nowait(error)
                except queue.Full:
                    pass

    def _read_stderr(self) -> None:
        assert self._process.stderr is not None
        try:
            for line in self._process.stderr:
                self._stderr.append(line)
                if sum(map(len, self._stderr)) > 16_384:
                    self._stderr = self._stderr[-32:]
        except (OSError, UnicodeError):
            return


def _regular_file(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise QualificationDriverError(f"{label} must be absolute")
    try:
        result = candidate.resolve(strict=True)
        info = result.stat()
    except OSError as error:
        raise QualificationDriverError(f"{label} is unavailable") from error
    if not result.is_file() or result.is_symlink() or info.st_nlink != 1:
        raise QualificationDriverError(f"{label} is not a unique regular file")
    return result


def _directory(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise QualificationDriverError(f"{label} must be absolute")
    try:
        result = candidate.resolve(strict=True)
    except OSError as error:
        raise QualificationDriverError(f"{label} is unavailable") from error
    if not result.is_dir():
        raise QualificationDriverError(f"{label} is not a directory")
    return result


def _json_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _object(payload: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload, object_pairs_hook=_unique_pairs)
    except (json.JSONDecodeError, ValueError) as error:
        raise QualificationDriverError(f"{label} is malformed") from error
    if not isinstance(value, dict):
        raise QualificationDriverError(f"{label} is not an object")
    return value


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
