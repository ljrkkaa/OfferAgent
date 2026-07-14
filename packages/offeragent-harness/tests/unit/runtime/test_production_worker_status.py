from __future__ import annotations

import pytest

from offeragent_harness.config import HarnessConfig
from offeragent_harness.permissions import PermissionMode
from offeragent_harness.protocol.common import PermissionMode as WirePermissionMode
from offeragent_harness.protocol.common import RunConfigSnapshot
from offeragent_harness.runtime.production_worker_composition import _effective_permission


@pytest.mark.parametrize(
    ("requested", "read_only", "trusted", "expected"),
    [
        (WirePermissionMode.NORMAL, False, False, PermissionMode.READ_ONLY),
        (WirePermissionMode.NORMAL, False, True, PermissionMode.NORMAL),
        (WirePermissionMode.TRUSTED_WORKSPACE, False, True, PermissionMode.TRUSTED_WORKSPACE),
        (WirePermissionMode.TRUSTED_WORKSPACE, False, False, PermissionMode.READ_ONLY),
        (WirePermissionMode.NORMAL, True, True, PermissionMode.READ_ONLY),
        (WirePermissionMode.PLAN, True, False, PermissionMode.PLAN),
    ],
)
def test_effective_permission_requires_explicit_workspace_trust_without_losing_plan(
    requested: WirePermissionMode,
    read_only: bool,
    trusted: bool,
    expected: PermissionMode,
) -> None:
    run = RunConfigSnapshot(model="gpt-test", permission_mode=requested)
    config = HarnessConfig.model_validate({"policy": {"read_only": read_only, "workspace_trusted": trusted}})
    assert _effective_permission(run, config) is expected
