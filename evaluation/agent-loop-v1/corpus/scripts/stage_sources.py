from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

import pymupdf

_MAXIMUM_PDF_BYTES = 128 * 1024 * 1024
_USER_AGENT = "OfferAgent literature evaluation/1.0 (local research corpus)"


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        value.get("schemaVersion") != 1
        or value.get("corpusId") != "offeragent-llm-literature-v1"
    ):
        raise ValueError("unsupported literature source manifest")
    documents = value.get("documents")
    if not isinstance(documents, list) or len(documents) != 30:
        raise ValueError("literature source manifest must contain exactly 30 documents")
    ids = [item.get("id") for item in documents]
    filenames = [item.get("filename") for item in documents]
    if len(set(ids)) != 30 or len(set(filenames)) != 30:
        raise ValueError("literature source identities and filenames must be unique")
    return documents


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _validate_pdf(path: Path, source: dict[str, Any]) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"source is not a regular PDF: {path}")
    size = path.stat().st_size
    if size != source["byteSize"] or size > _MAXIMUM_PDF_BYTES:
        raise ValueError(f"source size mismatch: {source['id']}")
    if _sha256(path) != source["sha256"]:
        raise ValueError(f"source hash mismatch: {source['id']}")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError(f"source signature is not PDF: {source['id']}")
    document = pymupdf.open(path)  # type: ignore[no-untyped-call]
    try:
        if document.page_count != source["pageCount"]:
            raise ValueError(f"source page count mismatch: {source['id']}")
        text_pages = sum(
            bool(document.load_page(index).get_text("text").strip())  # type: ignore[no-untyped-call]
            for index in range(document.page_count)
        )
    finally:
        document.close()  # type: ignore[no-untyped-call]
    if text_pages < 1:
        raise ValueError(f"source contains no embedded text: {source['id']}")
    return {
        "id": source["id"],
        "filename": source["filename"],
        "sha256": source["sha256"],
        "byteSize": size,
        "pageCount": source["pageCount"],
        "embeddedTextPageCount": text_pages,
        "acquisitionKind": source["acquisition"]["kind"],
        "officialUrl": source["officialUrl"],
    }


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with (
        urllib.request.urlopen(request, timeout=60) as response,
        destination.open("xb") as stream,
    ):
        total = 0
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > _MAXIMUM_PDF_BYTES:
                raise ValueError("downloaded PDF exceeds the corpus size limit")
            stream.write(block)
        stream.flush()
        os.fsync(stream.fileno())


def _materialize(source: dict[str, Any], *, projects_root: Path, output: Path) -> str:
    target = output / source["filename"]
    if target.exists():
        _validate_pdf(target, source)
        return "verified-existing"
    acquisition = source["acquisition"]
    candidate: Path | None = None
    if acquisition["kind"] == "projects-local":
        candidate = projects_root.joinpath(*acquisition["localPath"].split("/"))
        if candidate.is_file() and not candidate.is_symlink():
            _validate_pdf(candidate, source)
    with tempfile.TemporaryDirectory(
        prefix="offeragent-literature-", dir=output
    ) as temporary:
        staged = Path(temporary) / source["filename"]
        if candidate is not None and candidate.is_file():
            shutil.copyfile(candidate, staged)
            route = "projects-local"
        else:
            _download(source["pdfUrl"], staged)
            route = "official-download"
        _validate_pdf(staged, source)
        os.replace(staged, target)
    return route


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--projects-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    manifest = args.manifest.resolve(strict=True)
    projects_root = args.projects_root.resolve(strict=True)
    output = args.output.resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or any(path.is_symlink() for path in output.iterdir()):
        raise ValueError("literature output must be a non-symlink directory")
    documents = []
    for source in _read_manifest(manifest):
        route = _materialize(source, projects_root=projects_root, output=output)
        record = _validate_pdf(output / source["filename"], source)
        record["materialization"] = route
        documents.append(record)
    unexpected = sorted(
        path.name
        for path in output.iterdir()
        if path.is_file() and path.name not in {item["filename"] for item in documents}
    )
    if unexpected:
        raise ValueError(f"literature output contains unexpected files: {unexpected}")
    report = {
        "schemaVersion": 1,
        "corpusId": "offeragent-llm-literature-v1",
        "documentCount": len(documents),
        "totalPages": sum(
            item["pageCount"]
            for item in documents
            if isinstance(item["pageCount"], int)
        ),
        "projectsLocalCount": sum(
            item["acquisitionKind"] == "projects-local" for item in documents
        ),
        "arxivCount": sum(item["acquisitionKind"] == "arxiv" for item in documents),
        "generatedPdfCount": 0,
        "documents": documents,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
