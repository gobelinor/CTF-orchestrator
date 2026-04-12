from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from .cli import _extract_env_file_arg, _load_env_file
from .discord_sync import (
    CampaignDiscordObserver,
    ChallengeDiscordObserver,
    DiscordClient,
    DiscordDispatcher,
    resolve_discord_config,
)
from .importers import ImportRequest
from .supervisor import SupervisorRunRequest, run_supervisor


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = list(argv or sys.argv[1:])
    env_file = _extract_env_file_arg(argv)
    _load_env_file(env_file)

    parser = argparse.ArgumentParser(description="Run a board-level CTF campaign supervisor.")
    parser.add_argument("source", nargs="?", help="URL, local file path, or '-' for stdin.")
    parser.add_argument("--source-url", dest="source_url", help="Alias for the board source URL or path.")
    parser.add_argument("--input-file", type=Path, help="Read board text from a local file.")
    parser.add_argument("--session-cookie", help="Session cookie value or full Cookie header.")
    parser.add_argument("--cookie-file", type=Path, help="File containing the raw Cookie header.")
    parser.add_argument(
        "--start-instance-when-needed",
        action="store_true",
        help="Start CTFd challenge instances only when a scheduled challenge requires one.",
    )
    parser.add_argument("--category", action="append", default=[], help="Allowed challenge category. Repeatable.")
    parser.add_argument("--challenge", action="append", default=[], help="Allowed challenge title filter. Repeatable.")
    parser.add_argument(
        "--max-difficulty",
        choices=["easy", "medium", "hard"],
        help="Maximum explicit difficulty accepted by the scheduler.",
    )
    parser.add_argument("--max-challenges", type=int, help="Limit the campaign to the top N eligible challenges.")
    parser.add_argument(
        "--max-parallel-challenges",
        type=int,
        default=2,
        help="Maximum number of challenge runs executing at the same time.",
    )
    parser.add_argument(
        "--max-instance-challenges",
        type=int,
        default=1,
        help="Maximum number of instance-bound challenge runs executing at the same time.",
    )
    parser.add_argument(
        "--backend-sequence",
        default="mock",
        help="Comma-separated worker order, for example 'codex,claude'.",
    )
    parser.add_argument("--max-attempts", type=int, default=4, help="Maximum specialist attempts per challenge run.")
    parser.add_argument("--retry-needs-human", action="store_true", help="Retry challenges previously marked needs_human.")
    parser.add_argument("--skills-root", type=Path, default=Path("skills"), help="Skills directory.")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root under which .campaigns and .challenges will be created.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=env_file,
        help="Optional .env file loaded before parsing other options. Defaults to .env when present.",
    )
    parser.add_argument(
        "--discord-bot-token",
        default=os.getenv("DISCORD_BOT_TOKEN"),
        help="Discord bot token. Defaults to DISCORD_BOT_TOKEN.",
    )
    parser.add_argument(
        "--discord-parent-channel-id",
        default=os.getenv("DISCORD_PARENT_CHANNEL_ID"),
        help="Discord channel ID used as the parent for challenge threads and direct campaign updates.",
    )
    parser.add_argument(
        "--discord-auto-archive-duration",
        type=int,
        default=int(os.getenv("DISCORD_AUTO_ARCHIVE_DURATION", "10080")),
        help="Discord thread auto archive duration in minutes: 60, 1440, 4320 or 10080.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = args.source_url or args.source
    if not source and args.input_file is None:
        raise SystemExit("a board source is required via positional source, --source-url, or --input-file")

    if args.max_parallel_challenges < 1:
        raise SystemExit("--max-parallel-challenges must be >= 1")
    if args.max_instance_challenges < 1:
        raise SystemExit("--max-instance-challenges must be >= 1")

    try:
        discord_config = resolve_discord_config(
            bot_token=args.discord_bot_token,
            parent_channel_id=args.discord_parent_channel_id,
            auto_archive_duration=args.discord_auto_archive_duration,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    event_sink, finalize_event_sink = _build_event_sink(discord_config)
    try:
        result = run_supervisor(
            SupervisorRunRequest(
                import_request=ImportRequest(
                    source=source,
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
                backend_sequence=[item.strip() for item in args.backend_sequence.split(",") if item.strip()],
                max_attempts=args.max_attempts,
                categories=list(args.category),
                challenge_queries=list(args.challenge),
                max_difficulty=args.max_difficulty,
                max_challenges=args.max_challenges,
                max_parallel_challenges=args.max_parallel_challenges,
                max_instance_challenges=args.max_instance_challenges,
                retry_needs_human=bool(args.retry_needs_human),
                start_instance_when_needed=bool(args.start_instance_when_needed),
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


def _build_event_sink(discord_config) -> tuple[Any, Any]:
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
                print(f"[warning] challenge discord observer failed: {exc}", file=sys.stderr)
        if discord_config is None:
            return
        if campaign_observer is None and event_type == "campaign_started" and client is not None:
            campaign_observer = CampaignDiscordObserver(client, Path(str(payload["campaign_dir"])))
        if campaign_observer is not None:
            try:
                campaign_observer.handle_event(event_type, payload)
            except Exception as exc:
                print(f"[warning] campaign discord observer failed: {exc}", file=sys.stderr)

    def finalize() -> None:
        if dispatcher is not None:
            dispatcher.close()

    return sink, finalize


def _print_cli_event(event_type: str, payload: dict[str, Any]) -> None:
    if event_type == "campaign_started":
        print(
            f"[campaign] {payload.get('campaign_name')} from {payload.get('source_label')}",
            file=sys.stderr,
        )
        return
    if event_type == "campaign_import_completed":
        print(
            "[campaign] import completed: "
            f"discovered={payload.get('discovered', 0)} "
            f"eligible={payload.get('eligible', 0)} "
            f"skipped={payload.get('skipped', 0)} "
            f"import_failed={payload.get('import_failed', 0)}",
            file=sys.stderr,
        )
        return
    if event_type == "campaign_challenge_started":
        print(
            f"[start] {payload.get('challenge_name')} "
            f"(instance={'yes' if payload.get('instance_required') else 'no'})",
            file=sys.stderr,
        )
        return
    if event_type == "attempt_completed":
        print(
            f"[attempt] {payload.get('challenge_name')}: "
            f"{payload.get('backend')} -> {payload.get('status')}",
            file=sys.stderr,
        )
        return
    if event_type == "campaign_challenge_completed":
        print(
            f"[done] {payload.get('challenge_name')}: {payload.get('status')} "
            f"{payload.get('summary', '')}",
            file=sys.stderr,
        )
        return
    if event_type == "campaign_completed":
        counts = payload.get("counts", {})
        print(
            "[campaign] completed: "
            f"solved={counts.get('solved', 0)} "
            f"needs_human={counts.get('needs_human', 0)} "
            f"skipped={counts.get('skipped', 0)} "
            f"import_failed={counts.get('import_failed', 0)}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    sys.exit(main())
