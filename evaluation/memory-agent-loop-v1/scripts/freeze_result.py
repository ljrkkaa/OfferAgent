"""Freeze a completed memory Agent Loop evaluation into reviewable artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

REQUIRED = (
    "agent-loop-report.json",
    "run-results.jsonl",
    "raw-traces.jsonl",
    "tool-journal.sqlite",
)
TRACKED = frozenset({"agent-loop-report.json", "run-results.jsonl"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _jsonl_count(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number} is invalid JSON") from error
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} must contain an object")
            count += 1
    return count


def _entry(
    path: Path, *, tracked: bool, record_count: int | None = None
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "trackedByGit": tracked,
    }
    if record_count is not None:
        value["recordCount"] = record_count
    return value


def freeze(source: Path, destination: Path) -> Path:
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"refusing to replace frozen result: {destination}")
    missing = [name for name in REQUIRED if not (source / name).is_file()]
    if missing:
        raise FileNotFoundError(f"missing evaluation artifacts: {', '.join(missing)}")
    report = json.loads((source / "agent-loop-report.json").read_text(encoding="utf-8"))
    if not isinstance(report, dict) or not isinstance(report.get("turnRunCount"), int):
        raise ValueError("evaluation report is incompatible")
    expected = int(report["turnRunCount"])
    result_count = _jsonl_count(source / "run-results.jsonl")
    trace_count = _jsonl_count(source / "raw-traces.jsonl")
    if result_count != expected or trace_count != expected:
        raise ValueError(
            f"record count mismatch: results={result_count}, traces={trace_count}, expected={expected}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    try:
        for name in sorted(TRACKED):
            shutil.copyfile(source / name, destination / name)
        evaluation_root = Path(__file__).resolve().parents[1]
        repository = evaluation_root.parents[1]
        scenarios = evaluation_root / "scenarios.json"
        manifest = {
            "schemaVersion": 1,
            "benchmark": "offeragent-memory-agent-loop-v1",
            "status": "frozen",
            "turnRunCount": expected,
            "configFingerprint": report.get("integrity", {}).get("configFingerprint"),
            "sourceDirectory": source.as_posix(),
            "inputs": {
                "scenarios": {
                    "path": scenarios.resolve(strict=True)
                    .relative_to(repository)
                    .as_posix(),
                    "bytes": scenarios.stat().st_size,
                    "sha256": _sha256(scenarios),
                }
            },
            "artifacts": {
                name: _entry(
                    source / name,
                    tracked=name in TRACKED,
                    record_count=(
                        result_count
                        if name == "run-results.jsonl"
                        else trace_count
                        if name == "raw-traces.jsonl"
                        else None
                    ),
                )
                for name in REQUIRED
            },
        }
        manifest_path = destination / "artifact-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except Exception:
        shutil.rmtree(destination)
        raise
    return manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(freeze(args.source, args.destination))


if __name__ == "__main__":
    main()
