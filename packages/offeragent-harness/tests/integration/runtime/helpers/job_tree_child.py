from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    trigger = Path(sys.argv[1])
    child_pid = Path(sys.argv[2])
    grandchild_pid = Path(sys.argv[3])
    child_pid.write_text(str(os.getpid()), encoding="ascii")
    while not trigger.exists():
        time.sleep(0.01)
    grandchild = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        close_fds=True,
        creationflags=0x08000000,
    )
    grandchild_pid.write_text(str(grandchild.pid), encoding="ascii")
    time.sleep(300)


if __name__ == "__main__":
    main()
