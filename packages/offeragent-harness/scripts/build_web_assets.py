from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
MANIFEST = ROOT / "schema" / "protocol-manifest.json"
ASSETS = WEB / "assets"
HASH_PATTERN = re.compile(r'const SCHEMA_HASH = "sha256:[0-9a-f]{64}";')


def generated_files() -> dict[Path, bytes]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    schema_hash = manifest.get("schemaHash")
    if not isinstance(schema_hash, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", schema_hash) is None:
        raise RuntimeError("protocol manifest schemaHash is invalid")
    source = (WEB / "app.js").read_text(encoding="utf-8")
    replaced, count = HASH_PATTERN.subn(f'const SCHEMA_HASH = "{schema_hash}";', source)
    if count != 1:
        raise RuntimeError("Web app must contain exactly one generated schema identity")
    return {
        ASSETS / "app.js": replaced.encode("utf-8"),
        ASSETS / "app.css": (WEB / "styles.css").read_bytes(),
    }


def generate(*, check: bool) -> int:
    mismatches: list[str] = []
    for path, expected in generated_files().items():
        if check:
            if not path.exists() or path.read_bytes() != expected:
                mismatches.append(path.relative_to(ROOT).as_posix())
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected)
    if mismatches:
        raise SystemExit(f"Web assets are stale: {', '.join(mismatches)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "check"))
    args = parser.parse_args()
    return generate(check=args.command == "check")


if __name__ == "__main__":
    raise SystemExit(main())
