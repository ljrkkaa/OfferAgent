"""Validate frozen real-paper inputs and the built knowledge base.

This module intentionally computes no retrieval or answer-quality metrics. Those
belong exclusively to the production Agent Loop evaluation.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import fitz  # type: ignore[import-untyped]

from offeragent_harness.knowledge import (
    KnowledgeCatalogStore,
    KnowledgeObjectStore,
    KnowledgePublisher,
)

_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.IGNORECASE)
_EXPECTED_TYPES = {
    "alias_to_title": 30,
    "excerpt_to_title": 30,
    "multi_source_alias": 5,
    "passage_completion": 30,
    "unanswerable": 5,
}


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not all(isinstance(item, dict) for item in records):
        raise ValueError(f"expected JSON objects: {path}")
    return records


def _mapping(value: object, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(message)
    return value


def _records(value: object, message: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(message)
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _tokens(text: str) -> set[str]:
    dehyphenated = re.sub(
        r"(?<=\w)-\s*[\r\n]+\s*(?=\w)", "", text.replace("\u00ad", "")
    )
    return {match.group(0).casefold() for match in _TOKEN.finditer(dehyphenated)}


def _pdf_pages(path: Path) -> tuple[str, ...]:
    with path.open("rb") as stream:
        _check(stream.read(5) == b"%PDF-", f"not a PDF: {path.name}")
    document = fitz.open(path)
    try:
        return tuple(
            document[index].get_text(sort=True) for index in range(len(document))
        )
    finally:
        document.close()


def _metadata_literal_leaks(
    paths: list[Path], documents: list[dict[str, Any]]
) -> list[str]:
    metadata: set[str] = set()
    tags = {str(tag).casefold() for document in documents for tag in document["tags"]}
    for document in documents:
        metadata.update(
            str(value).casefold()
            for value in (
                document["title"],
                document["arxivId"],
                *document["aliases"],
            )
        )
    leaks: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value.strip().casefold() in metadata:
                leaks.append(f"{path.name}:{node.lineno}:{node.value!r}")
        for statement in tree.body:
            value = (
                statement.value
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                else None
            )
            if not isinstance(value, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
                continue
            for node in ast.walk(value):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value.strip().casefold() in tags
                ):
                    leaks.append(f"{path.name}:{node.lineno}:{node.value!r}")
    return sorted(leaks)


def _source_validation(
    *, root: Path, manifest: dict[str, Any], source_report: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, tuple[str, ...]], int]:
    documents = _records(manifest.get("documents"), "source documents are invalid")
    _check(manifest.get("schemaVersion") == 1, "source schema is invalid")
    _check(len(documents) == 30, "expected 30 sources")
    ids = [str(item["id"]) for item in documents]
    filenames = [str(item["filename"]) for item in documents]
    _check(
        len(set(ids)) == 30 and len(set(filenames)) == 30, "source identities repeat"
    )
    acquisitions = Counter(str(item["acquisition"]["kind"]) for item in documents)
    _check(
        acquisitions == {"arxiv": 9, "projects-local": 21}, "acquisition mix changed"
    )

    pages_by_source: dict[str, tuple[str, ...]] = {}
    total_pages = 0
    for item in documents:
        filename = str(item["filename"])
        path = (root / "generated" / "vault" / "raw" / filename).resolve(strict=True)
        _check(path.stat().st_size == item["byteSize"], f"size mismatch: {filename}")
        _check(_sha256(path) == item["sha256"], f"hash mismatch: {filename}")
        pages = _pdf_pages(path)
        _check(len(pages) == item["pageCount"], f"page count mismatch: {filename}")
        _check(
            all(text.strip() for text in pages), f"embedded text missing: {filename}"
        )
        pages_by_source[f"raw/{filename}"] = pages
        total_pages += len(pages)

    _check(total_pages == 1230, "corpus page total changed")
    _check(source_report.get("generatedPdfCount") == 0, "generated PDFs are forbidden")
    _check(source_report.get("documentCount") == 30, "source report count mismatch")
    _check(source_report.get("arxivCount") == 9, "source report arXiv count mismatch")
    _check(source_report.get("projectsLocalCount") == 21, "local source count mismatch")
    _check(
        source_report.get("totalPages") == total_pages, "source report page mismatch"
    )
    return documents, pages_by_source, total_pages


def _question_validation(
    *,
    root: Path,
    pages_by_source: dict[str, tuple[str, ...]],
    corpus_tokens: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    questions_path = root / "generated" / "questions.jsonl"
    questions = _read_jsonl(questions_path)
    report = _read_json(root / "generated" / "results" / "question-report.json")
    _check(len(questions) == 100, "expected 100 questions")
    _check(len({str(item["id"]) for item in questions}) == 100, "question IDs repeat")
    counts = Counter(str(item["type"]) for item in questions)
    _check(counts == _EXPECTED_TYPES, "question type distribution changed")
    _check(report.get("questionCount") == 100, "question report count mismatch")
    _check(
        report.get("typeCounts") == _EXPECTED_TYPES, "question report types mismatch"
    )
    _check(
        report.get("sha256") == _sha256(questions_path),
        "question report hash mismatch",
    )

    for question in questions:
        answer = str(question["answer"])
        evidence = _records(question["evidence"], f"invalid evidence: {question['id']}")
        if question["type"] == "unanswerable":
            _check(
                not answer and not evidence,
                f"unanswerable gold is not empty: {question['id']}",
            )
            nonce_tokens = _tokens(str(question["question"])) - {
                "what",
                "does",
                "the",
                "corpus",
                "term",
                "denote",
            }
            _check(
                nonce_tokens.isdisjoint(corpus_tokens),
                f"nonce leaked into corpus: {question['id']}",
            )
            continue
        _check(
            bool(answer) and bool(evidence),
            f"answerable gold is empty: {question['id']}",
        )
        supporting_text: list[str] = []
        for citation in evidence:
            source_path = str(citation["sourcePath"])
            pages = pages_by_source.get(source_path)
            if pages is None:
                raise ValueError(f"unknown gold source: {source_path}")
            page_numbers = citation["pages"]
            _check(
                isinstance(page_numbers, list) and bool(page_numbers),
                f"gold pages missing: {question['id']}",
            )
            if not isinstance(page_numbers, list):
                raise AssertionError("unreachable page-number validation")
            _check(
                len(page_numbers) == len(set(page_numbers)),
                f"gold pages repeat: {question['id']}",
            )
            for page_number in page_numbers:
                _check(
                    isinstance(page_number, int) and 1 <= page_number <= len(pages),
                    f"gold page out of range: {question['id']}",
                )
                supporting_text.append(pages[page_number - 1])
        _check(
            _tokens(answer) <= _tokens("\n".join(supporting_text)),
            f"gold answer is not present in its PDF evidence: {question['id']}",
        )
    return questions, report


def _knowledge_validation(
    *, root: Path, documents: list[dict[str, Any]], total_pages: int
) -> dict[str, int]:
    vault = (root / "generated" / "vault").resolve(strict=True)
    state = vault / ".offeragent" / "knowledge"
    catalog_store = KnowledgeCatalogStore(state)
    object_store = KnowledgeObjectStore(state)
    catalog = catalog_store.load()
    _check(
        catalog.revision == 1 and len(catalog.sources) == 30, "catalog is incomplete"
    )
    manifest_by_path = {f"raw/{item['filename']}": item for item in documents}
    node_count = 0
    object_page_count = 0
    for source in catalog.sources:
        item = manifest_by_path.get(source.relative_path)
        if item is None:
            raise ValueError(
                f"catalog source is not in manifest: {source.relative_path}"
            )
        _check(source.content_hash == item["sha256"], "catalog source hash mismatch")
        _check(source.byte_size == item["byteSize"], "catalog source size mismatch")
        prepared = object_store.load(source.source_id, source.content_hash)
        _check(len(prepared.pages) == item["pageCount"], "object page count mismatch")
        _check(
            prepared.page_index.page_count == len(prepared.pages),
            "PageIndex page mismatch",
        )
        node_count += len(prepared.page_index.nodes)
        object_page_count += len(prepared.pages)
    _check(object_page_count == total_pages, "knowledge object page total mismatch")

    publisher = KnowledgePublisher(
        vault_root=vault, catalog=catalog_store, objects=object_store
    )
    wiki_ids = publisher.existing_wiki_ids()
    indexes = publisher.current_page_indexes()
    _check(len(indexes) == 30, "published PageIndex count mismatch")
    _check(
        all(node.summary.strip() for tree in indexes for node in tree.nodes),
        "published PageIndex summary is empty",
    )
    _check(len(wiki_ids) == 144, "published Wiki page count mismatch")
    registry = _read_json(vault / "knowledge" / ".pages.json")
    pages = _records(registry.get("pages"), "Wiki registry is invalid")
    _check(len(pages) == 144, "Wiki registry is incomplete")
    _check(len({str(item["path"]) for item in pages}) == 144, "Wiki paths repeat")
    _check(
        all((vault / "knowledge" / str(item["path"])).is_file() for item in pages),
        "Wiki file is missing",
    )
    return {"catalogSources": 30, "pageIndexNodes": node_count, "wikiPages": 144}


def _report_validation(
    *, root: Path, expected_gpu: str | None, total_pages: int
) -> dict[str, Any]:
    build = _read_json(root / "generated" / "results" / "build-report.json")
    expected_build = {
        "catalogRevision": 1,
        "conceptPageCount": 84,
        "ocrPageCount": 0,
        "pageIndexCount": 30,
        "sourceCount": 30,
        "totalPageCount": total_pages,
        "wikiPageCount": 144,
    }
    for name, value in expected_build.items():
        _check(build.get(name) == value, f"build report mismatch: {name}")
    built_sources = _records(build.get("sources"), "build source report is invalid")
    _check(len(built_sources) == 30, "build source report is incomplete")
    cache_hits = sum(bool(item.get("cacheHit")) for item in built_sources)
    _check(
        build.get("cacheHitCount") == cache_hits,
        "build cache-hit count is inconsistent",
    )
    runtime = _mapping(build.get("runtimeParser"), "runtime parser report is missing")
    _check(
        runtime.get("ocrExecutionProvider") == "cuda", "build was not CUDA-configured"
    )
    _check(
        "CPU rasterization" in str(runtime.get("pdf")), "PyMuPDF execution is misstated"
    )

    gpu = _read_json(root / "generated" / "results" / "gpu-ocr-report.json")
    _check(gpu.get("status") == "passed", "GPU OCR acceptance did not pass")
    gpu_identity = _mapping(gpu.get("gpu"), "GPU identity is missing")
    gpu_name = str(gpu_identity.get("name"))
    if expected_gpu is not None:
        _check(gpu_name == expected_gpu, f"unexpected GPU: {gpu_name}")
    onnx = _mapping(gpu.get("onnxRuntime"), "ONNX Runtime report is missing")
    parser = _mapping(gpu.get("parser"), "parser report is missing")
    inference = _mapping(gpu.get("inference"), "inference report is missing")
    _check(onnx.get("device") == "GPU", "ONNX Runtime is not a GPU build")
    _check(
        "CUDAExecutionProvider" in onnx.get("availableProviders", []),
        "CUDA provider is unavailable",
    )
    _check(parser.get("ocrExecutionProvider") == "cuda", "parser OCR is not CUDA")
    _check(
        _mapping(parser.get("backend"), "OCR backend report is missing").get("name")
        == "rapidocr:bgr:cuda",
        "wrong OCR backend",
    )
    expected_tokens = set(inference.get("acceptanceTokens", []))
    _check(
        expected_tokens <= set(str(inference.get("recognizedText", "")).split()),
        "GPU OCR acceptance text is incomplete",
    )
    return {"gpu": gpu_name, "ocrSeconds": inference.get("elapsedSeconds")}


def validate(
    *, root: Path, package_root: Path, expected_gpu: str | None
) -> dict[str, Any]:
    manifest = _read_json(root / "sources.json")
    lock = _read_json(root / "corpus-lock.json")
    locked_corpus = _mapping(lock.get("corpus"), "corpus lock is invalid")
    _check(
        locked_corpus.get("sourceManifestSha256") == _sha256(root / "sources.json"),
        "source manifest differs from the frozen corpus lock",
    )
    source_report = _read_json(root / "generated" / "results" / "source-report.json")
    documents, pages_by_source, total_pages = _source_validation(
        root=root, manifest=manifest, source_report=source_report
    )
    corpus_tokens = _tokens(
        "\n".join(text for pages in pages_by_source.values() for text in pages)
    )
    questions, _ = _question_validation(
        root=root, pages_by_source=pages_by_source, corpus_tokens=corpus_tokens
    )
    locked_questions = _mapping(
        lock.get("questions"), "locked question metadata is invalid"
    )
    _check(
        locked_questions.get("sha256")
        == _sha256(root / "generated" / "questions.jsonl"),
        "question set differs from the frozen corpus lock",
    )
    knowledge = _knowledge_validation(
        root=root, documents=documents, total_pages=total_pages
    )
    runtime = _report_validation(
        root=root, expected_gpu=expected_gpu, total_pages=total_pages
    )
    _check(
        lock.get("knowledge") == knowledge,
        "knowledge projection differs from the frozen corpus lock",
    )

    scan_paths = sorted(
        (package_root / "src" / "offeragent_harness" / "knowledge").glob("*.py")
    )
    leaks = _metadata_literal_leaks(scan_paths, documents)
    _check(
        not leaks,
        "corpus metadata is hardcoded in production knowledge code: "
        + "; ".join(leaks),
    )
    return {
        "schemaVersion": 1,
        "status": "passed",
        "corpus": {
            "realPdfCount": len(documents),
            "generatedPdfCount": 0,
            "totalPages": total_pages,
        },
        "questions": {"count": len(questions), "typeCounts": _EXPECTED_TYPES},
        "knowledge": knowledge,
        "runtime": runtime,
        "hardcodedCorpusMetadataLeaks": leaks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--expected-gpu")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("generated/results/corpus-validation-report.json"),
    )
    args = parser.parse_args()
    report = validate(
        root=args.root.resolve(strict=True),
        package_root=args.package_root.resolve(strict=True),
        expected_gpu=args.expected_gpu,
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
