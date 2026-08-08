"""Verify that the frozen Windows process host executes RapidOCR through CUDA."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from offeragent_harness.documents import (
    CanonicalParseFailure,
    DocumentMediaType,
    DocumentParseRequest,
    DocumentSource,
    decode_canonical_response,
    encode_canonical_request,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _gpu_identity() -> dict[str, str]:
    completed = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    fields = [part.strip() for part in completed.stdout.splitlines()[0].split(",")]
    if len(fields) != 3:
        raise RuntimeError("nvidia-smi returned an unexpected GPU identity")
    return {"name": fields[0], "driverVersion": fields[1], "memoryMiB": fields[2]}


def _acceptance_image(path: Path) -> None:
    image = Image.new("RGB", (1900, 560), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=140)
    draw.text((60, 75), "OFFERAGENT FROZEN", font=font, fill="black")
    draw.text((60, 300), "RAPIDOCR CUDA", font=font, fill="black")
    image.save(path, format="PNG")


def _minimal_environment(scratch: Path) -> dict[str, str]:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve(strict=True)
    return {
        "PATH": str(system_root / "System32"),
        "SystemRoot": str(system_root),
        "TEMP": str(scratch),
        "TMP": str(scratch),
        "WINDIR": str(system_root),
    }


def verify(*, runtime: Path, scratch: Path) -> dict[str, object]:
    executable = (runtime / "offeragent-process-host.exe").resolve(strict=True)
    manifest_path = (runtime / "development-runtime-manifest.json").resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("frozen Runtime manifest is invalid")
    scratch.mkdir(parents=True, exist_ok=False)
    document_root = scratch / "document-000"
    document_root.mkdir()
    source = document_root / "source.bin"
    _acceptance_image(source)
    source_hash = _sha256(source)
    request = encode_canonical_request(
        DocumentParseRequest(
            request_id="frozen-gpu-ocr-acceptance",
            source=DocumentSource(
                source_id="frozen-gpu-ocr-acceptance-image",
                absolute_path=source,
                declared_media_type=DocumentMediaType.PNG,
                expected_sha256=source_hash,
            ),
        )
    )
    started = time.perf_counter()
    completed = subprocess.run(
        [str(executable), "document-extract"],
        cwd=scratch,
        input=request,
        capture_output=True,
        check=False,
        env=_minimal_environment(scratch),
        timeout=120,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        raise RuntimeError(
            f"frozen process host failed ({completed.returncode}): "
            f"{completed.stderr.decode('utf-8', errors='replace').strip()}"
        )
    if completed.stderr:
        raise RuntimeError("frozen process host emitted unexpected stderr")
    response = decode_canonical_response(completed.stdout)
    if isinstance(response, CanonicalParseFailure):
        raise RuntimeError(f"frozen OCR failed: {response.code.value}")
    if len(response.result.pages) != 1:
        raise RuntimeError("frozen OCR returned an unexpected page count")
    page = response.result.pages[0].page
    backend = page.provenance.extraction_backend
    expected_tokens = {"OFFERAGENT", "FROZEN", "RAPIDOCR", "CUDA"}
    recognized_tokens = set(page.text.upper().split())
    if backend.name != "rapidocr:bgr:cuda":
        raise RuntimeError(f"frozen parser used the wrong backend: {backend.name}")
    if not expected_tokens <= recognized_tokens:
        raise RuntimeError("frozen CUDA OCR acceptance text is incomplete")
    return {
        "schemaVersion": 1,
        "status": "passed",
        "gpu": _gpu_identity(),
        "runtime": {
            "buildCommit": manifest.get("build", {}).get("commit"),
            "runtimeVersion": manifest.get("runtimeVersion"),
            "manifestSha256": _sha256(manifest_path),
            "processHostSha256": _sha256(executable),
        },
        "parser": {
            "backend": backend.name,
            "backendVersion": backend.version,
            "backendModel": backend.model,
            "extractionMethod": page.extraction_method.value,
        },
        "inference": {
            "elapsedSeconds": round(elapsed, 6),
            "recognizedText": page.text,
            "acceptanceTokens": sorted(expected_tokens),
        },
        "fixture": {
            "kind": "generated PNG frozen-runtime acceptance fixture; not a knowledge source",
            "sha256": source_hash,
            "byteSize": source.stat().st_size,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify(
        runtime=args.runtime.resolve(strict=True),
        scratch=args.scratch.resolve(strict=False),
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
