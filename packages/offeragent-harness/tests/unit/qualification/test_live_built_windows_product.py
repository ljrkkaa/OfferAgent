from __future__ import annotations

# ruff: noqa: RUF001 -- Chinese punctuation is semantic fixture evidence.
import hashlib
import os
import stat
from pathlib import Path

import pytest
from scripts.qualify_live_built_windows_product import (
    LiveBuiltProductQualificationError,
    _canonical_json,
    _remove_owned_root,
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


def test_owned_root_cleanup_refuses_a_changed_marker(tmp_path: Path) -> None:
    root = tmp_path / "qualification"
    root.mkdir()
    expected = _canonical_json({"schemaVersion": 1, "token": "a" * 64})
    (root / ".offeragent-qualification-owner.json").write_bytes(
        _canonical_json({"schemaVersion": 1, "token": "b" * 64})
    )

    with pytest.raises(LiveBuiltProductQualificationError, match="ownership identity differs"):
        _remove_owned_root(root, expected)
