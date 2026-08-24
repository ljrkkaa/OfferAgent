from __future__ import annotations

import os

import pytest

from offeragent_harness.runtime.runtime_manifest import native_windows_architecture


@pytest.mark.skipif(os.name != "nt", reason="native architecture probe is Windows-only")
def test_native_windows_architecture_uses_the_real_os_machine() -> None:
    assert native_windows_architecture() == "x64"
