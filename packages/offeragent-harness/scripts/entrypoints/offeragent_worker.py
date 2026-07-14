from __future__ import annotations

import json
import os
import sys

if sys.argv[1:] == ["image-probe", "--canonical-json"]:
    sys.stdout.write(json.dumps({"pid": os.getpid(), "role": "worker"}, sort_keys=True, separators=(",", ":")) + "\n")
    raise SystemExit(0)

# The release gate deliberately imports this mandatory production module.  A
# build cannot silently substitute testing fakes or a partial command table.
from offeragent_harness.runtime.production_worker_composition import main

raise SystemExit(main(sys.argv[1:]))
