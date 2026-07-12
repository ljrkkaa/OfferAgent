from __future__ import annotations

import argparse
from collections.abc import Sequence

from offeragent_harness import __version__
from offeragent_harness.protocol import schemas


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="offeragent-harness")
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    schema = subcommands.add_parser("schema", help="generate or verify the canonical protocol schema")
    schema.add_argument("schema_command", choices=("generate", "check", "hash"))
    schema.add_argument("--output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "schema":
        schema_argv = [args.schema_command]
        if args.output is not None:
            schema_argv.extend(("--output", args.output))
        return schemas.main(schema_argv)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
