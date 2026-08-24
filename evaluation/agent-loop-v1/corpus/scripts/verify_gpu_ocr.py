"""Run a real RapidOCR inference through CUDA and emit machine-readable evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from offeragent_harness.documents import (
    DocumentMediaType,
    DocumentParseRequest,
    DocumentSource,
)
from offeragent_harness.documents.adapters import build_bundled_parser
from offeragent_harness.runtime.document_parser_profile import (
    BUNDLED_DOCUMENT_PARSER_CONFIG,
)


class _CancellationToken:
    def checkpoint(self) -> None:
        return


def _gpu_identity() -> dict[str, str]:
    result = subprocess.run(
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
    fields = [part.strip() for part in result.stdout.splitlines()[0].split(",")]
    if len(fields) != 3:
        raise RuntimeError("nvidia-smi returned an unexpected GPU identity")
    return {"name": fields[0], "driverVersion": fields[1], "memoryMiB": fields[2]}


def _write_acceptance_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1800, 560), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=150)
    draw.text((70, 80), "OFFERAGENT GPU", font=font, fill="black")
    draw.text((70, 290), "RAPIDOCR 3050", font=font, fill="black")
    image.save(path, format="PNG")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("generated/results/gpu-ocr-report.json"),
    )
    parser.add_argument(
        "--image",
        type=Path,
        default=Path("generated/tmp/gpu-ocr-smoke.png"),
    )
    args = parser.parse_args()
    image_path = args.image.resolve()
    output_path = args.output.resolve()
    _write_acceptance_image(image_path)

    import onnxruntime as ort  # type: ignore[import-untyped]

    ort.preload_dlls()
    providers = list(ort.get_available_providers())
    if ort.get_device().upper() != "GPU" or "CUDAExecutionProvider" not in providers:
        raise RuntimeError("ONNX Runtime CUDAExecutionProvider is unavailable")

    source_bytes = image_path.read_bytes()
    started = time.perf_counter()
    document_parser = build_bundled_parser(BUNDLED_DOCUMENT_PARSER_CONFIG)
    parsed = document_parser.parse(
        DocumentParseRequest(
            request_id="gpu-ocr-acceptance",
            source=DocumentSource(
                source_id="gpu-ocr-acceptance-image",
                absolute_path=image_path,
                declared_media_type=DocumentMediaType.PNG,
                expected_sha256=f"sha256:{hashlib.sha256(source_bytes).hexdigest()}",
            ),
        ),
        _CancellationToken(),
    )
    elapsed_seconds = time.perf_counter() - started
    page = parsed.pages[0]
    backend = page.provenance.extraction_backend
    normalized = " ".join(parsed.text.upper().split())
    for required in ("OFFERAGENT", "GPU", "RAPIDOCR", "3050"):
        if required not in normalized:
            raise RuntimeError(f"RapidOCR result omitted acceptance token: {required}")
    if backend.name != "rapidocr:bgr:cuda":
        raise RuntimeError(f"unexpected OCR backend identity: {backend.name}")

    report: dict[str, Any] = {
        "schemaVersion": 1,
        "status": "passed",
        "gpu": _gpu_identity(),
        "onnxRuntime": {
            "version": ort.__version__,
            "device": ort.get_device(),
            "availableProviders": providers,
            "requiredProvider": "CUDAExecutionProvider",
            "deviceId": BUNDLED_DOCUMENT_PARSER_CONFIG.ocr_device_id,
        },
        "parser": {
            "configFingerprint": parsed.parser_config_fingerprint,
            "ocrExecutionProvider": BUNDLED_DOCUMENT_PARSER_CONFIG.ocr_execution_provider,
            "backend": backend.to_json(),
            "extractionMethod": page.extraction_method.value,
        },
        "inference": {
            "elapsedSeconds": round(elapsed_seconds, 6),
            "recognizedText": parsed.text,
            "regionCount": len(page.ocr_regions),
            "acceptanceTokens": ["OFFERAGENT", "GPU", "RAPIDOCR", "3050"],
        },
        "fixture": {
            "kind": "generated PNG infrastructure acceptance fixture; not a knowledge source",
            "sha256": f"sha256:{hashlib.sha256(source_bytes).hexdigest()}",
            "byteSize": len(source_bytes),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(output_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
