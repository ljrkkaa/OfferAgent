from __future__ import annotations

from pathlib import Path

from scripts.check_architecture import check


def _package(tmp_path: Path, provider_source: str) -> Path:
    package = tmp_path / "src" / "offeragent_harness"
    (package / "agent").mkdir(parents=True)
    (package / "providers").mkdir()
    (package / "agent" / "loop.py").write_text(
        "@agent_loop_entrypoint\ndef run():\n    return None\n",
        encoding="utf-8",
    )
    (package / "providers" / "adapter.py").write_text(provider_source, encoding="utf-8")
    return tmp_path


def test_architecture_check_covers_the_real_model_provider_adapter_directory(tmp_path: Path) -> None:
    root = _package(tmp_path, "import subprocess\n")

    problems = check(root)

    assert len(problems) == 1
    assert problems[0].replace("\\", "/") == ("providers/adapter.py: model boundary imports ['subprocess']")


def test_architecture_check_accepts_an_inference_only_provider(tmp_path: Path) -> None:
    root = _package(tmp_path, "from collections.abc import AsyncIterator\n")

    assert check(root) == []
