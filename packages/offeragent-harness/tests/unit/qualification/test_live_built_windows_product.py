from __future__ import annotations

# ruff: noqa: RUF001 -- Chinese punctuation is semantic fixture evidence.
import hashlib
import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.qualify_built_windows_product import _remove_owned_root as _remove_built_owned_root
from scripts.qualify_live_built_windows_product import (
    LiveBuiltProductQualificationError,
    _canonical_json,
    _remove_owned_root,
    _vault_markdown_documents,
    _vault_markdown_snapshot,
    qualify_live_built_windows_product,
)

from offeragent_harness.qualification.synthetic_interview import (
    SyntheticInterviewSemanticError,
    validate_synthetic_interview_vault,
)


def _semantic_vault() -> dict[str, str]:
    experience = "experiences/stellar-backend.md"
    questions = (
        "interview/redis-cache-breakdown.md",
        "interview/payment-callback-idempotency.md",
        "interview/database-slow-query.md",
        "interview/canary-rollback.md",
    )
    question_links = "\n".join(f"- [[../{path.removesuffix('.md')}]]" for path in questions)
    experience_link = f"[[../{experience.removesuffix('.md')}]]"
    return {
        experience: (
            "---\ntype: interview-experience\ncompany: 星河科技（虚构）\nrole: 后端工程师\n"
            "round: 一面、二面、终面\n---\n## Questions\n"
            f"{question_links}\n"
        ),
        "experiences/index.md": f"- [[{experience.removesuffix('.md')}]]\n",
        questions[0]: (
            "---\ntype: interview-question\nanswer-state: needs-research\nfrequency: 1\n---\n"
            f"Redis 缓存击穿\n{experience_link}\n"
        ),
        questions[1]: (
            "---\ntype: interview-question\nanswer-state: needs-research\nfrequency: 1\n---\n"
            f"订单创建成功后，支付回调重复到达时，如何保证幂等并避免重复扣款？\n{experience_link}\n"
        ),
        questions[2]: (
            "---\ntype: interview-question\nanswer-state: needs-research\nfrequency: 1\n---\n"
            f"如何定位数据库慢查询？\n{experience_link}\n"
        ),
        questions[3]: (
            "---\ntype: interview-question\nanswer-state: needs-research\nfrequency: 1\n---\n"
            f"灰度发布失败后，如何设计快速回滚与验证方案？\n{experience_link}\n"
        ),
        "interview/index.md": "\n".join(f"- [[{path.removesuffix('.md')}]]" for path in questions) + "\n",
    }


def test_built_product_semantic_gate_rejects_structurally_valid_but_wrong_vault_content() -> None:
    valid = _semantic_vault()
    reviewed_hashes = {
        path: f"sha256:{hashlib.sha256(content.encode()).hexdigest()}" for path, content in valid.items()
    }
    validate_synthetic_interview_vault(valid, tuple(valid), reviewed_content_hashes=reviewed_hashes)
    wrong = dict(valid)
    wrong["interview/payment-callback-idempotency.md"] = wrong["interview/payment-callback-idempotency.md"].replace(
        "支付回调重复到达", "任意占位问题"
    )
    wrong_hashes = {path: f"sha256:{hashlib.sha256(content.encode()).hexdigest()}" for path, content in wrong.items()}

    with pytest.raises(SyntheticInterviewSemanticError, match="cross-page Q2"):
        validate_synthetic_interview_vault(wrong, tuple(wrong), reviewed_content_hashes=wrong_hashes)


def test_live_semantic_gate_reads_markdown_bodies_separately_from_replay_hashes(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    valid = _semantic_vault()
    for relative, content in valid.items():
        target = vault / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode())

    bodies = _vault_markdown_documents(vault)
    hashes = _vault_markdown_snapshot(vault)

    validate_synthetic_interview_vault(bodies, tuple(bodies), reviewed_content_hashes=hashes)
    with pytest.raises(SyntheticInterviewSemanticError, match="final content"):
        validate_synthetic_interview_vault(hashes, tuple(hashes), reviewed_content_hashes=hashes)


def test_owned_root_cleanup_clears_read_only_git_objects(tmp_path: Path) -> None:
    root = tmp_path / "qualification"
    git_object = root / "Vault" / ".git" / "objects" / "aa" / "object"
    git_object.parent.mkdir(parents=True)
    git_object.write_bytes(b"sealed git object")
    os.chmod(git_object, stat.S_IREAD)
    marker_payload = _canonical_json({"schemaVersion": 1, "token": "a" * 64})
    (root / ".offeragent-qualification-owner.json").write_bytes(marker_payload)

    _remove_owned_root(root, marker_payload)

    assert not root.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path regression")
@pytest.mark.parametrize("remove_owned_root", (_remove_owned_root, _remove_built_owned_root))
def test_owned_root_cleanup_removes_paths_beyond_the_legacy_windows_limit(
    tmp_path: Path,
    remove_owned_root: Callable[[Path, bytes], None],
) -> None:
    root = tmp_path / "qualification"
    deep_parent = root / "Vault" / ".obsidian" / "plugins" / "offeragent-obsidian-plugin" / "runtime"
    leaf = "a" * (270 - len(str(deep_parent)) - 1)
    deep_file = deep_parent / leaf
    assert len(str(deep_file)) == 270
    filesystem_file = Path(f"\\\\?\\{deep_file}")
    filesystem_file.parent.mkdir(parents=True)
    filesystem_file.write_bytes(b"deep-runtime-asset")
    marker_payload = _canonical_json({"schemaVersion": 1, "token": "a" * 64})
    (root / ".offeragent-qualification-owner.json").write_bytes(marker_payload)

    remove_owned_root(root, marker_payload)

    assert not root.exists()


def test_owned_root_cleanup_refuses_a_changed_marker(tmp_path: Path) -> None:
    root = tmp_path / "qualification"
    root.mkdir()
    expected = _canonical_json({"schemaVersion": 1, "token": "a" * 64})
    (root / ".offeragent-qualification-owner.json").write_bytes(
        _canonical_json({"schemaVersion": 1, "token": "b" * 64})
    )

    with pytest.raises(LiveBuiltProductQualificationError, match="ownership identity differs"):
        _remove_owned_root(root, expected)


def test_live_failure_still_audits_auth_processes_and_owned_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugin"
    templates = plugin / "migration" / "target-vault" / "obsidian-cli"
    templates.mkdir(parents=True)
    (templates.parent / "agent.md").write_text("# Agent\n", encoding="utf-8")
    (templates / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
    qualification = tmp_path / "qualification"
    qualification.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text('{"access_token":"opaque"}\n', encoding="utf-8")
    paired = SimpleNamespace(
        plugin_root=plugin,
        qualification_root=qualification,
        driver=qualification / "driver.cjs",
        plugin_version="2.0.0-test",
        runtime_version="0.1.0-test",
        runtime_manifest_sha256="sha256:" + "a" * 64,
        source_commit="c" * 40,
        source_tree_sha256="sha256:" + "b" * 64,
    )
    process_snapshots: Iterator[dict[str, set[int]]] = iter(
        (
            {
                "offeragent-worker.exe": set[int](),
                "offeragent-process-host.exe": set[int](),
                "obsidian.exe": set[int](),
            },
            {
                "offeragent-worker.exe": {4242},
                "offeragent-process-host.exe": set[int](),
                "obsidian.exe": set[int](),
            },
        )
    )

    monkeypatch.setattr("scripts.qualify_live_built_windows_product.verify_paired_windows_artifacts", lambda *_: paired)
    monkeypatch.setattr("scripts.qualify_live_built_windows_product.default_codex_auth_path", lambda: auth)
    monkeypatch.setattr(
        "scripts.qualify_live_built_windows_product._product_process_ids", lambda: next(process_snapshots)
    )
    monkeypatch.setattr("scripts.qualify_live_built_windows_product._initialize_vault_git", lambda *_: None)

    def fail_after_auth_drift(_self: object, _root: Path) -> object:
        auth.write_text('{"access_token":"changed"}\n', encoding="utf-8")
        raise OSError("primary qualification failure")

    monkeypatch.setattr(
        "scripts.qualify_live_built_windows_product.SyntheticInterviewFixtureGenerator.generate",
        fail_after_auth_drift,
    )

    with pytest.raises(LiveBuiltProductQualificationError) as captured:
        qualify_live_built_windows_product(
            plugin,
            qualification,
            source_root_guard=source,
            node_executable=tmp_path / "node.exe",
            proxy_url="http://127.0.0.1:7896",
            model="gpt-5.5",
            temporary_parent=runs,
        )

    message = str(captured.value)
    assert "primary qualification failure" in message
    assert "auth" in message.casefold()
    assert "offeragent-worker.exe:4242" in message
    assert list(runs.iterdir()) == []
