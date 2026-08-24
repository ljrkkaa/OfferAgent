"""Freeze a completed Agent Loop evaluation into reviewable Git artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


REQUIRED_ARTIFACTS = (
    "agent-loop-report.json",
    "run-results.jsonl",
    "raw-traces.jsonl",
    "tool-journal.sqlite",
)
TRACKED_ARTIFACTS = frozenset({"agent-loop-report.json", "run-results.jsonl"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _count_jsonl(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} is not valid JSON") from exc
            count += 1
    return count


def _load_report(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _artifact_entry(
    path: Path, *, tracked: bool, record_count: int | None = None
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "trackedByGit": tracked,
    }
    if record_count is not None:
        entry["recordCount"] = record_count
    return entry


def _evaluation_inputs() -> dict[str, Path]:
    evaluation_root = Path(__file__).resolve().parents[1]
    return {
        "evaluationPlan": evaluation_root / "evaluation-plan.json",
        "knowledgeQuestions": evaluation_root
        / "corpus"
        / "generated"
        / "questions.jsonl",
        "routingControls": evaluation_root / "routing-controls.jsonl",
        "sourceManifest": evaluation_root / "corpus" / "sources.json",
    }


def _input_entry(path: Path, repository: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    return {
        "bytes": resolved.stat().st_size,
        "path": resolved.relative_to(repository).as_posix(),
        "sha256": _sha256(resolved),
    }


def freeze_result(source: Path, destination: Path, expected_runs: int) -> Path:
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"refusing to replace frozen result: {destination}")

    missing = [name for name in REQUIRED_ARTIFACTS if not (source / name).is_file()]
    if missing:
        raise FileNotFoundError(f"missing evaluation artifacts: {', '.join(missing)}")

    report_path = source / "agent-loop-report.json"
    results_path = source / "run-results.jsonl"
    traces_path = source / "raw-traces.jsonl"
    report = _load_report(report_path)
    if report.get("runCount") != expected_runs:
        raise ValueError(
            f"report has {report.get('runCount')} runs, expected {expected_runs}"
        )

    results_count = _count_jsonl(results_path)
    traces_count = _count_jsonl(traces_path)
    if results_count != expected_runs or traces_count != expected_runs:
        raise ValueError(
            f"record count mismatch: results={results_count}, traces={traces_count}, expected={expected_runs}"
        )

    destination.mkdir(parents=True, exist_ok=True)
    try:
        for name in sorted(TRACKED_ARTIFACTS):
            shutil.copyfile(source / name, destination / name)

        fingerprint = report.get("integrity", {}).get("configFingerprint")
        repository = Path(__file__).resolve().parents[3]
        manifest = {
            "artifacts": {
                name: _artifact_entry(
                    source / name,
                    tracked=name in TRACKED_ARTIFACTS,
                    record_count=(
                        results_count
                        if name == "run-results.jsonl"
                        else traces_count
                        if name == "raw-traces.jsonl"
                        else None
                    ),
                )
                for name in REQUIRED_ARTIFACTS
            },
            "benchmark": "offeragent-agent-loop-v1",
            "configFingerprint": fingerprint,
            "inputs": {
                name: _input_entry(path, repository)
                for name, path in sorted(_evaluation_inputs().items())
            },
            "runCount": expected_runs,
            "schemaVersion": 1,
            "sourceDirectory": source.as_posix(),
            "status": "frozen",
        }
        manifest_path = destination / "artifact-manifest.json"
        with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            )
    except Exception:
        shutil.rmtree(destination)
        raise
    return destination / "artifact-manifest.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--expected-runs", type=int, default=150)
    args = parser.parse_args()
    manifest = freeze_result(args.source, args.destination, args.expected_runs)
    print(manifest)


if __name__ == "__main__":
    main()
