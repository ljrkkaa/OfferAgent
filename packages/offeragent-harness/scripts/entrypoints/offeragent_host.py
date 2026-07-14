from __future__ import annotations

import asyncio
import json
import os
import sys

from offeragent_harness.runtime.host_cli import main

if sys.argv[1:] == ["image-probe", "--canonical-json"]:
    sys.stdout.write(json.dumps({"pid": os.getpid(), "role": "host"}, sort_keys=True, separators=(",", ":")) + "\n")
    raise SystemExit(0)
if sys.argv[1:] == ["control-probe", "--canonical-json"]:
    from offeragent_harness.runtime.production_host_composition import create_production_host_application

    async def probe() -> None:
        application = create_production_host_application(self_test_nonce=f"{os.getpid():016x}")
        await application.self_test_control_start_stop()

    try:
        asyncio.run(probe())
    except BaseException:
        raise SystemExit(2) from None
    sys.stdout.write(
        json.dumps(
            {"pid": os.getpid(), "role": "host", "status": "control-ready"},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    raise SystemExit(0)
raise SystemExit(main())
