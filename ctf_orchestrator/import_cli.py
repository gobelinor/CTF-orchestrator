from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .cli import _extract_env_file_arg, _load_env_file
from .import_service import import_selected_candidates, load_board_context, validate_instance_access
from .importers import DiscoveredChallenge, ImportRequest, render_import_review
from .workspace import _slugify


def list_discovered_challenges(candidates: list[DiscoveredChallenge]) -> str:
    lines: list[str] = []
    for index, candidate in enumerate(candidates, 1):
        details: list[str] = []
        if candidate.challenge_id is not None:
            details.append(f"id={candidate.challenge_id}")
        if candidate.points is not None:
            details.append(f"{candidate.points} pts")
        if candidate.solves is not None:
            details.append(f"{candidate.solves} solves")
        suffix = f" ({', '.join(details)})" if details else ""
        warnings = ", ".join(str(w) for w in (candidate.warnings or []))
        warning_suffix = f"  [warnings: {warnings}]" if warnings else ""
        lines.append(f"[{index}] {candidate.title}{suffix}{warning_suffix}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = list(argv or sys.argv[1:])
    env_file = _extract_env_file_arg(argv)
    _load_env_file(env_file)

    parser = argparse.ArgumentParser(description="Import a CTF challenge source into the project JSON format.")
    parser.add_argument("source", nargs="?", help="URL, local file path, or '-' for stdin.")
    parser.add_argument("--input-file", type=Path, help="Read challenge text from a local file.")
    parser.add_argument("--output", type=Path, help="Write the imported challenge JSON to this path.")
    parser.add_argument("--stdout", action="store_true", help="Print the final JSON payload to stdout.")
    parser.add_argument("--review", action="store_true", help="Print a short import review to stderr.")
    parser.add_argument("--list", action="store_true", help="List discovered challenge candidates and exit.")
    parser.add_argument(
        "--challenge",
        help="Select one discovered challenge by exact or partial title when the source contains multiple challenges.",
    )
    parser.add_argument(
        "--session-cookie",
        help="Session cookie value or full Cookie header used when fetching a protected challenge URL.",
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        help="File containing the raw Cookie header value used for authenticated challenge pages.",
    )
    parser.add_argument(
        "--start-instance",
        action="store_true",
        help="For CTFd container challenges, start the selected instance before building the JSON output.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=env_file,
        help="Optional .env file loaded before parsing other options. Defaults to .env when present.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    import_request = ImportRequest(
        source=args.source,
        input_file=args.input_file.resolve() if args.input_file else None,
        output=args.output.resolve() if args.output else None,
        use_stdout=bool(args.stdout),
        review=bool(args.review),
        selected_challenge=args.challenge,
        list_only=bool(args.list),
        session_cookie=args.session_cookie,
        cookie_file=args.cookie_file.resolve() if args.cookie_file else None,
        start_instance=bool(args.start_instance),
    )
    context = load_board_context(import_request)

    if args.list:
        print(list_discovered_challenges(context.candidates))
        return 0

    records = import_selected_candidates(
        context,
        queries=[args.challenge] if args.challenge else None,
        start_instance=bool(args.start_instance),
    )
    if not records:
        raise SystemExit("No challenge-like content was detected in the source.")
    if len(records) != 1:
        raise SystemExit("ctf-import requires a single selected challenge when not using --list.")

    record = records[0]
    imported = record.imported
    payload = record.payload
    if imported is None or payload is None:
        print(f"[error] {record.error or 'challenge import failed'}", file=sys.stderr)
        return 2

    if import_request.review:
        print(render_import_review(imported), file=sys.stderr)
    instance_error = validate_instance_access(import_request, imported)
    if instance_error:
        print(f"[error] {instance_error}", file=sys.stderr)
        return 2

    output_path = import_request.output
    if output_path is None and not import_request.use_stdout:
        output_path = Path("examples") / f"{_slugify(imported.title)}.json"

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[info] wrote {output_path}", file=sys.stderr)

    if import_request.use_stdout or output_path is None:
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
