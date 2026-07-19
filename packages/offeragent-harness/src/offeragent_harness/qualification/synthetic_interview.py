"""Committed fictional image fixture shared by built-product qualifications."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SyntheticInterviewPage:
    index: int
    path: Path
    content_hash: str
    media_type: str
    byte_length: int


@dataclass(frozen=True, slots=True)
class SyntheticInterviewFixture:
    pages: tuple[SyntheticInterviewPage, ...]
    fixture_sha256: str


class SyntheticInterviewFixtureGenerator:
    """Materialize three hash-pinned PNGs without fonts or runtime rendering."""

    _PAGES = (
        (1, "page-1.png.b64", "sha256:58fc39a75f39afd5dee5ef1eb50aadacdb5fdaeb7ddce67b807cc9aa97fc9e0d"),
        (2, "page-2.png.b64", "sha256:e818a610b96125c112e4c22c0a54f93239bfbe37a711fa19ef0c7dd567e240f3"),
        (3, "page-3.png.b64", "sha256:e6b3fe971fbe73b19d4b40854525b1ae49acfd0c9ab9e5145ba1e5da7b8f0bba"),
    )

    def generate(self, root: Path) -> SyntheticInterviewFixture:
        if not root.is_absolute():
            raise ValueError("qualification fixture root must be absolute")
        root.mkdir(parents=True, exist_ok=False)
        pages: list[SyntheticInterviewPage] = []
        fixture_root = resources.files("offeragent_harness.qualification").joinpath("fixtures")
        for index, fixture_name, expected_hash in self._PAGES:
            encoded = fixture_root.joinpath(fixture_name).read_bytes()
            try:
                payload = base64.b64decode(b"".join(encoded.split()), validate=True)
            except ValueError as error:
                raise RuntimeError("qualification fixture encoding is invalid") from error
            actual_hash = f"sha256:{hashlib.sha256(payload).hexdigest()}"
            if actual_hash != expected_hash or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("qualification fixture content differs from its pinned identity")
            path = root / f"page-{index}.png"
            path.write_bytes(payload)
            pages.append(
                SyntheticInterviewPage(
                    index=index,
                    path=path,
                    content_hash=actual_hash,
                    media_type="image/png",
                    byte_length=len(payload),
                )
            )
        manifest = json.dumps(
            {"pages": [{"index": page.index, "sha256": page.content_hash} for page in pages]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return SyntheticInterviewFixture(
            tuple(pages),
            f"sha256:{hashlib.sha256(manifest).hexdigest()}",
        )


class SyntheticInterviewSemanticError(RuntimeError):
    """The built product did not preserve the fixture's known interview facts."""


def validate_synthetic_interview_vault(
    snapshot: Mapping[str, str],
    changed_paths: Sequence[str],
    *,
    reviewed_content_hashes: Mapping[str, str] | None = None,
) -> None:
    """Validate the seven reviewed writes against independently known fixture facts."""

    paths = tuple(changed_paths)
    if len(paths) != 7 or len(paths) != len(set(paths)):
        raise SyntheticInterviewSemanticError("synthetic interview must produce exactly seven changed paths")
    changed = set(paths)
    experience_paths = sorted(
        path for path in changed if path.startswith("experiences/") and path != "experiences/index.md"
    )
    question_paths = sorted(path for path in changed if path.startswith("interview/") and path != "interview/index.md")
    if (
        len(experience_paths) != 1
        or len(question_paths) != 4
        or changed != {*experience_paths, *question_paths, "experiences/index.md", "interview/index.md"}
    ):
        raise SyntheticInterviewSemanticError("synthetic interview changed-path topology is invalid")
    if any(path not in snapshot or not isinstance(snapshot[path], str) for path in changed):
        raise SyntheticInterviewSemanticError("synthetic interview changed content is unavailable")
    if reviewed_content_hashes is not None:
        if set(reviewed_content_hashes) != changed:
            raise SyntheticInterviewSemanticError("synthetic interview reviewed target set differs")
        for path in paths:
            content_hash = f"sha256:{hashlib.sha256(snapshot[path].encode()).hexdigest()}"
            if reviewed_content_hashes[path] != content_hash:
                raise SyntheticInterviewSemanticError("synthetic interview final content differs from its review")

    experience_path = experience_paths[0]
    experience = snapshot[experience_path]
    for fact in ("星河科技", "后端工程师", "一面", "二面", "终面"):
        if fact not in experience:
            raise SyntheticInterviewSemanticError(f"synthetic interview Experience omitted {fact}")

    topics = (
        ("Q1", ("Redis", "缓存击穿")),
        ("cross-page Q2", ("订单创建成功", "支付回调重复到达", "幂等", "重复扣款")),
        ("Q3", ("数据库", "慢查询")),
        ("Q4", ("灰度发布", "回滚", "验证")),
    )
    matched_questions: set[str] = set()
    for label, facts in topics:
        matches = [path for path in question_paths if all(fact in snapshot[path] for fact in facts)]
        if len(matches) != 1:
            raise SyntheticInterviewSemanticError(f"synthetic interview {label} semantic evidence is invalid")
        matched_questions.add(matches[0])
    if len(matched_questions) != 4:
        raise SyntheticInterviewSemanticError("synthetic interview topics did not map to four distinct Questions")

    experience_stem = Path(experience_path).stem
    experience_index = snapshot["experiences/index.md"]
    if "[[" not in experience_index or experience_stem not in experience_index:
        raise SyntheticInterviewSemanticError("synthetic interview Experience index link is missing")
    question_index = snapshot["interview/index.md"]
    for path in question_paths:
        question = snapshot[path]
        stem = Path(path).stem
        if "answer-state: needs-research" not in question or "frequency: 1" not in question:
            raise SyntheticInterviewSemanticError("synthetic interview Question state is invalid")
        if "[[" not in question or experience_stem not in question:
            raise SyntheticInterviewSemanticError("synthetic interview Question-to-Experience link is missing")
        if "[[" not in experience or stem not in experience:
            raise SyntheticInterviewSemanticError("synthetic interview Experience-to-Question link is missing")
        if "[[" not in question_index or stem not in question_index:
            raise SyntheticInterviewSemanticError("synthetic interview Question index link is missing")


__all__ = [
    "SyntheticInterviewFixture",
    "SyntheticInterviewFixtureGenerator",
    "SyntheticInterviewPage",
    "SyntheticInterviewSemanticError",
    "validate_synthetic_interview_vault",
]
