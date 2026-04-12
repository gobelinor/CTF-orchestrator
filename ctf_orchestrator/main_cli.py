from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from .discord_sync import (
    CampaignDiscordObserver,
    ChallengeDiscordObserver,
    DiscordClient,
    DiscordDispatcher,
    resolve_discord_config,
)
from .importers import ImportRequest
from .supervisor import SupervisorRunRequest, run_supervisor
from .utils import extract_env_file_arg, load_env_file


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = list(argv or sys.argv[1:])
    env_file = extract_env_file_arg(argv)
    load_env_file(env_file)

    parser = argparse.ArgumentParser(
        prog="ctf",
        description="CTF orchestrator — run a campaign against a board of challenges.",
    )
    parser.add_argument("source", nargs="?", help="URL, local file path, or '-' for stdin.")
    parser.add_argument("--input-file", type=Path, help="Read board text from a local file.")
    parser.add_argument("--session-cookie", help="Session cookie value or full Cookie header.")
    parser.add_argument("--cookie-file", type=Path, help="File containing the raw Cookie header.")
    parser.add_argument("--category", action="append", default=[], help="Allowed category. Repeatable.")
    parser.add_argument("--challenge", action="append", default=[], help="Challenge title filter. Repeatable.")
    parser.add_argument("--max-difficulty", choices=["easy", "medium", "hard"], help="Maximum difficulty.")
    parser.add_argument("--max-challenges", type=int, help="Limit to top N eligible challenges.")
    parser.add_argument("--max-parallel", type=int, default=2, help="Max parallel challenge runs (default 2).")
    parser.add_argument(
        "--backend-sequence",
        default="mock",
        help="Comma-separated worker order, e.g. 'claude,codex'.",
    )
    parser.add_argument("--max-attempts", type=int, default=4, help="Max specialist attempts per challenge.")
    parser.add_argument("--skills-root", type=Path, default=Path("skills"), help="Skills directory.")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace root.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=env_file,
        help="Optional .env file. Defaults to .env when present.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.source and args.input_file is None:
        raise SystemExit("a board source is required (positional URL/path or --input-file)")

    try:
        discord_config = resolve_discord_config(
            bot_token=os.getenv("DISCORD_BOT_TOKEN"),
            parent_channel_id=os.getenv("DISCORD_PARENT_CHANNEL_ID"),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    event_sink, finalize_event_sink = _build_event_sink(discord_config)
    try:
        result = run_supervisor(
            SupervisorRunRequest(
                import_request=ImportRequest(
                    source=args.source,
                    input_file=args.input_file.resolve() if args.input_file else None,
                    output=None,
                    use_stdout=False,
                    review=False,
                    selected_challenge=None,
                    list_only=False,
                    session_cookie=args.session_cookie,
                    cookie_file=args.cookie_file.resolve() if args.cookie_file else None,
                    start_instance=False,
                ),
                workspace_root=args.workspace,
                skills_root=args.skills_root,
                backend_sequence=[b.strip() for b in args.backend_sequence.split(",") if b.strip()],
                max_attempts=args.max_attempts,
                categories=list(args.category),
                challenge_queries=list(args.challenge),
                max_difficulty=args.max_difficulty,
                max_challenges=args.max_challenges,
                max_parallel_challenges=args.max_parallel,
                max_instance_challenges=1,
                retry_needs_human=False,
                start_instance_when_needed=True,
            ),
            event_sink=event_sink,
        )
    finally:
        finalize_event_sink()

    output = {
        "campaign_dir": str(result.campaign_dir),
        "summary_path": str(result.campaign_dir / "summary.md"),
        "counts": result.state.counts_by_status(),
    }
    print(json.dumps(output, indent=2))
    return 0


def _build_event_sink(discord_config: Any) -> tuple[Any, Any]:
    client = None
    dispatcher = None
    challenge_observer = None
    campaign_observer = None
    if discord_config is not None:
        client = DiscordClient(discord_config)
        dispatcher = DiscordDispatcher(client)
        challenge_observer = ChallengeDiscordObserver(client, dispatcher)

    def sink(event_type: str, payload: dict[str, Any]) -> None:
        nonlocal campaign_observer
        _print_cli_event(event_type, payload)
        if challenge_observer is not None:
            try:
                challenge_observer.handle_event(event_type, payload)
            except Exception as exc:
                print(f"[warning] discord: {exc}", file=sys.stderr)
        if discord_config is None:
            return
        if campaign_observer is None and event_type == "campaign_started" and client is not None:
            campaign_observer = CampaignDiscordObserver(client, Path(str(payload["campaign_dir"])))
        if campaign_observer is not None:
            try:
                campaign_observer.handle_event(event_type, payload)
            except Exception as exc:
                print(f"[warning] discord: {exc}", file=sys.stderr)

    def finalize() -> None:
        if dispatcher is not None:
            dispatcher.close()

    return sink, finalize


def _print_cli_event(event_type: str, payload: dict[str, Any]) -> None:
    if event_type == "campaign_started":
        print(f"[campaign] {payload.get('campaign_name')} from {payload.get('source_label')}", file=sys.stderr)
    elif event_type == "campaign_import_completed":
        print(
            f"[campaign] import: discovered={payload.get('discovered', 0)} "
            f"eligible={payload.get('eligible', 0)} skipped={payload.get('skipped', 0)}",
            file=sys.stderr,
        )
    elif event_type == "campaign_challenge_started":
        print(f"[start] {payload.get('challenge_name')}", file=sys.stderr)
    elif event_type == "attempt_completed":
        print(f"[attempt] {payload.get('challenge_name')}: {payload.get('backend')} -> {payload.get('status')}", file=sys.stderr)
    elif event_type == "campaign_challenge_completed":
        print(f"[done] {payload.get('challenge_name')}: {payload.get('status')}", file=sys.stderr)
    elif event_type == "campaign_completed":
        counts = payload.get("counts", {})
        print(f"[campaign] done: solved={counts.get('solved', 0)} needs_human={counts.get('needs_human', 0)}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
