from __future__ import annotations

import pytest

from offeragent_harness.cli import main
from offeragent_harness.protocol.schemas import schema_hash


def test_cli_reports_canonical_schema_hash(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["schema", "hash"]) == 0
    assert capsys.readouterr().out.strip() == schema_hash()
