"""Build a deterministic, page-grounded evaluation set from the real papers."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import fitz  # type: ignore[import-untyped]

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’\-]*")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_CONTRIBUTION_CUES = (
    "we introduce",
    "we present",
    "we propose",
    "this paper",
    "this work",
    "our approach",
    "our method",
)


def _normalize_pdf_text(text: str) -> str:
    """Remove extraction-only line-wrap hyphenation without changing real compounds."""

    return re.sub(r"(?<=\w)-\s*[\r\n]+\s*(?=\w)", "", text.replace("\u00ad", ""))


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    documents = value.get("documents") if isinstance(value, dict) else None
    if value.get("schemaVersion") != 1 or not isinstance(documents, list):
        raise ValueError("source manifest is invalid")
    if len(documents) != 30:
        raise ValueError("literature evaluation requires exactly 30 sources")
    return documents


def _document_text(path: Path) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    document = fitz.open(path)
    try:
        pages: list[str] = []
        page_blocks: list[tuple[str, ...]] = []
        for index in range(len(document)):
            page = document[index]
            pages.append(page.get_text(sort=True))
            page_blocks.append(
                tuple(
                    str(block[4])
                    for block in page.get_text("blocks", sort=True)
                    if len(block) >= 5 and isinstance(block[4], str) and str(block[4]).strip()
                )
            )
        return tuple(pages), tuple(page_blocks)
    finally:
        document.close()


def _candidate_sentences(page_blocks: tuple[tuple[str, ...], ...]) -> tuple[tuple[int, str], ...]:
    ranked: list[tuple[int, int, int, str]] = []
    for page_number, blocks in enumerate(page_blocks[:3], start=1):
        position = 0
        for raw in blocks:
            normalized = " ".join(_normalize_pdf_text(raw).split())
            abstract = re.search(r"\babstract\b", normalized, flags=re.IGNORECASE)
            search_start = abstract.end() if abstract is not None else 0
            region = normalized[search_start:]
            for sentence in _SENTENCE_BOUNDARY.split(region):
                sentence = sentence.strip()
                words = _WORD.findall(sentence)
                position += len(sentence) + 1
                if (
                    len(words) < 18
                    or len(words) > 90
                    or len(sentence) < 120
                    or len(sentence) > 750
                    or "http" in sentence.casefold()
                    or "@" in sentence
                ):
                    continue
                cue_score = sum(cue in sentence.casefold() for cue in _CONTRIBUTION_CUES)
                ranked.append((-cue_score, page_number, position, sentence))
    ranked.sort()
    selected: list[tuple[int, str]] = []
    seen: set[str] = set()
    for _, page_number, _, sentence in ranked:
        identity = sentence.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        selected.append((page_number, sentence))
        if len(selected) == 2:
            return tuple(selected)
    raise ValueError("paper does not contain two suitable opening-page sentences")


def _completion(sentence: str) -> tuple[str, str]:
    matches = tuple(_WORD.finditer(sentence))
    answer_words = min(12, max(8, len(matches) // 4))
    boundary = matches[-answer_words].start()
    prefix = sentence[:boundary].rstrip()
    answer = sentence[boundary:].strip()
    if len(_WORD.findall(prefix)) < 10 or len(_WORD.findall(answer)) < 8:
        raise ValueError("passage completion could not be bounded")
    return prefix, answer


def _gold(filename: str, pages: list[int]) -> list[dict[str, object]]:
    return [{"sourcePath": f"raw/{filename}", "pages": sorted(set(pages))}]


def _supporting_pages(pages: tuple[str, ...], answer: str) -> list[int]:
    expected = {token.casefold() for token in _WORD.findall(answer)}
    supported = [
        page_number
        for page_number, text in enumerate(pages[:3], start=1)
        if expected
        <= {token.casefold() for token in _WORD.findall(_normalize_pdf_text(text))}
    ]
    if not supported:
        raise ValueError("gold answer is not supported by the opening evidence pages")
    return supported


def build_questions(
    *, manifest_path: Path, raw_root: Path
) -> tuple[list[dict[str, object]], dict[str, object]]:
    documents = _read_manifest(manifest_path)
    questions: list[dict[str, object]] = []
    title_page_cache: dict[str, list[int]] = {}
    for ordinal, item in enumerate(documents, start=1):
        filename = str(item["filename"])
        title = str(item["title"])
        aliases = item["aliases"]
        if (
            not isinstance(aliases, list)
            or not aliases
            or not all(isinstance(alias, str) and alias for alias in aliases)
        ):
            raise ValueError("paper aliases are invalid")
        pages, page_blocks = _document_text((raw_root / filename).resolve(strict=True))
        candidates = _candidate_sentences(page_blocks)
        title_pages = _supporting_pages(pages, title)
        title_page_cache[str(item["id"])] = title_pages
        excerpt_page, excerpt_sentence = candidates[0]
        completion_page, completion_sentence = candidates[1]
        prefix, answer = _completion(completion_sentence)
        questions.extend(
            (
                {
                    "id": f"q-{ordinal:03d}-alias",
                    "type": "alias_to_title",
                    "question": f'What is the full paper title commonly referred to as "{aliases[0]}"?',
                    "answer": title,
                    "evidence": _gold(filename, title_pages),
                },
                {
                    "id": f"q-{ordinal:03d}-excerpt",
                    "type": "excerpt_to_title",
                    "question": (
                        "Which paper contains this opening-page passage: "
                        f'"{excerpt_sentence[:320]}"?'
                    ),
                    "answer": title,
                    "evidence": _gold(filename, [*title_pages, excerpt_page]),
                },
                {
                    "id": f"q-{ordinal:03d}-completion",
                    "type": "passage_completion",
                    "question": (
                        f'In "{title}", complete this opening-page passage: '
                        f'"{prefix} ____"'
                    ),
                    "answer": answer,
                    "evidence": _gold(
                        filename,
                        [completion_page, *_supporting_pages(pages, answer)],
                    ),
                },
            )
        )

    for pair_index in range(5):
        left = documents[pair_index * 2]
        right = documents[pair_index * 2 + 1]
        left_alias = str(left["aliases"][0])
        right_alias = str(right["aliases"][0])
        questions.append(
            {
                "id": f"q-091-pair-{pair_index + 1}",
                "type": "multi_source_alias",
                "question": (
                    f'What are the full titles of the two papers known as "{left_alias}" '
                    f'and "{right_alias}"?'
                ),
                "answer": f"{left['title']}; {right['title']}",
                "evidence": [
                    {
                        "sourcePath": f"raw/{left['filename']}",
                        "pages": title_page_cache[str(left["id"])],
                    },
                    {
                        "sourcePath": f"raw/{right['filename']}",
                        "pages": title_page_cache[str(right["id"])],
                    },
                ],
            }
        )

    for index in range(5):
        nonce = hashlib.sha256(f"absent-literature-{index}".encode()).hexdigest()[:18]
        questions.append(
            {
                "id": f"q-096-unanswerable-{index + 1}",
                "type": "unanswerable",
                "question": f'What does the corpus term "zqxv-{nonce}" denote?',
                "answer": "",
                "evidence": [],
            }
        )

    if len(questions) != 100 or len({str(item["id"]) for item in questions}) != 100:
        raise RuntimeError("question set cardinality is invalid")
    encoded = "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
        for item in questions
    ).encode("utf-8")
    report: dict[str, object] = {
        "schemaVersion": 1,
        "questionCount": len(questions),
        "typeCounts": dict(
            sorted(Counter(str(item["type"]) for item in questions).items())
        ),
        "sourceDocumentCount": len(documents),
        "derivation": "deterministic aliases and opening-page text; no model-generated facts",
        "sha256": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
    }
    return questions, report


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("sources.json"))
    parser.add_argument("--raw", type=Path, default=Path("generated/vault/raw"))
    parser.add_argument(
        "--output", type=Path, default=Path("generated/questions.jsonl")
    )
    parser.add_argument(
        "--report", type=Path, default=Path("generated/results/question-report.json")
    )
    args = parser.parse_args()
    questions, report = build_questions(
        manifest_path=args.manifest.resolve(strict=True),
        raw_root=args.raw.resolve(strict=True),
    )
    payload = "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
        for item in questions
    ).encode("utf-8")
    _atomic_write(args.output.resolve(), payload)
    _atomic_write(
        args.report.resolve(),
        (
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
