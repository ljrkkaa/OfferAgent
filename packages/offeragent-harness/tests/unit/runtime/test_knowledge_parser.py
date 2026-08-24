from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from offeragent_harness.documents import decode_canonical_request
from offeragent_harness.foundation.canonical import canonical_json_bytes
from offeragent_harness.knowledge import SourceRecord
from offeragent_harness.ports import CancellationToken, SupervisedProcessRequest, SupervisedProcessResult
from offeragent_harness.runtime.knowledge_parser import KnowledgeProcessParser, KnowledgeProcessParserConfig
from offeragent_harness.testing import ManualCancellationToken

_PARSER_FINGERPRINT = "sha256:" + "c" * 64
_EXECUTABLE_FINGERPRINT = "sha256:" + "d" * 64


class _Clock:
    def utcnow(self) -> datetime:
        return datetime(2026, 8, 7, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return 1.0

    async def sleep_until(self, deadline: datetime) -> None:
        del deadline


class _Process:
    def __init__(self) -> None:
        self.requests: list[SupervisedProcessRequest] = []

    async def execute(
        self, request: SupervisedProcessRequest, cancellation: CancellationToken
    ) -> SupervisedProcessResult:
        cancellation.checkpoint()
        self.requests.append(request)
        parsed = decode_canonical_request(request.stdin)
        content = parsed.source.absolute_path.read_bytes()
        text = "Parser-observed page."
        backend = {"name": "fixture", "version": "1.0"}
        response = canonical_json_bytes(
            {
                "ok": True,
                "requestId": parsed.request_id,
                "result": {
                    "pageCount": 1,
                    "pages": [
                        {
                            "characterCount": len(text),
                            "extractionMethod": "embedded_text",
                            "ocrRegions": [],
                            "provenance": {
                                "extractionBackend": backend,
                                "inputBackend": backend,
                                "pageNumber": 1,
                                "sourceId": parsed.source.source_id,
                                "sourceSha256": parsed.source.expected_sha256,
                            },
                            "text": text,
                            "utf8ByteCount": len(text.encode()),
                            "utf8EndByte": len(text.encode()),
                            "utf8StartByte": 0,
                        }
                    ],
                    "parserConfigFingerprint": _PARSER_FINGERPRINT,
                    "source": {
                        "byteSize": len(content),
                        "mediaType": parsed.source.declared_media_type.value,
                        "sha256": parsed.source.expected_sha256,
                        "sourceId": parsed.source.source_id,
                    },
                    "text": text,
                    "totalCharacters": len(text),
                    "totalTextUtf8Bytes": len(text.encode()),
                },
                "schemaVersion": 1,
            }
        )
        return SupervisedProcessResult(0, response, b"")


async def test_knowledge_parser_uses_the_canonical_supervised_document_host(tmp_path: Path) -> None:
    working = tmp_path / "working"
    working.mkdir()
    content = b"%PDF fixture"
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(content)
    source_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    source = SourceRecord("src-1234567890abcdef", "raw/source.pdf", source_hash, "application/pdf", len(content))
    process = _Process()
    parser = KnowledgeProcessParser(
        workspace_id="ws-test",
        process_supervisor=process,
        scratch_working_root=working,
        clock=_Clock(),
        config=KnowledgeProcessParserConfig(_PARSER_FINGERPRINT, _EXECUTABLE_FINGERPRINT),
    )

    result = await parser.parse(
        source=source,
        absolute_path=source_path,
        cancellation=ManualCancellationToken(),
    )

    assert result.source_hash == source_hash
    assert result.pages[0].text == "Parser-observed page."
    assert process.requests[0].executable_id == "document-extract"
    assert process.requests[0].allow_artifact_spill is False
    assert not any(working.iterdir())
