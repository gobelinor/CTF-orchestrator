from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ctf",
        description="CTF orchestrator — solve, import, or run campaigns.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Solve a single challenge.")
    sub.add_parser("import", help="Import challenges from a source.")
    sub.add_parser("campaign", help="Run a board-level campaign.")

    args, remaining = parser.parse_known_args(argv)

    if args.command == "run":
        from .cli import main as run_main
        return run_main(remaining)
    if args.command == "import":
        from .import_cli import main as import_main
        return import_main(remaining)
    if args.command == "campaign":
        from .supervisor_cli import main as campaign_main
        return campaign_main(remaining)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
