from __future__ import annotations

import os

import pytest

from offeragent_harness.runtime.windows_security import (
    WindowsProcessIdentityState,
    windows_process_identity_state,
)


@pytest.mark.skipif(os.name != "nt", reason="Windows process-token identity is Windows-only")
def test_current_process_is_proven_to_belong_to_current_sid() -> None:
    assert windows_process_identity_state(os.getpid()) is WindowsProcessIdentityState.CURRENT_USER


def test_invalid_process_identity_is_rejected_before_win32_call() -> None:
    with pytest.raises(ValueError, match="positive Windows DWORD"):
        windows_process_identity_state(0)
