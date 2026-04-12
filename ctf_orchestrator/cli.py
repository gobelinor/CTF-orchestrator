from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .challenges import normalize_challenge_payload
from .discord_sync import ChallengeDiscordObserver, DiscordClient, DiscordDispatcher, resolve_discord_config
from .orchestrator_service import ChallengeRunRequest, run_challenge


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    argv = list(argv or sys.argv[1:])
    env_file = _extract_env_file_arg(argv)
    _load_env_file(env_file)

    parser = argparse.ArgumentParser(description="LangGraph CTF orchestrator PoC.")
    parser.add_argument("--challenge-file", type=Path, help="JSON file describing the challenge.")
    parser.add_argument("--challenge-name", help="Override challenge name.")
    parser.add_argument("--challenge-text", help="Override challenge text.")
    parser.add_argument("--category-hint", help="Optional explicit challenge category.")
    parser.add_argument("--artifact", action="append", default=[], help="Artifact path. Repeatable.")
    parser.add_argument(
        "--backend-sequence",
        default="mock",
        help="Comma-separated worker order, for example 'codex,claude'.",
    )
    parser.add_argument("--max-attempts", type=int, default=4, help="Maximum specialist attempts.")
    parser.add_argument(
        "--parallel-trajectories",
        type=int,
        default=1,
        help="Spawn N isolated trajectories for the same challenge; first-flag-wins.",
    )
    parser.add_argument("--skills-root", type=Path, default=Path("skills"), help="Skills directory.")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root under which a dedicated challenge directory will be created.",
    )
    parser.add_argument("--thread-id", default="ctf-poc", help="LangGraph thread ID.")
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
        help="Discord channel ID used to create challenge threads.",
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
    challenge = normalize_challenge_payload(_load_challenge_file(args.challenge_file)) if args.challenge_file else {}
    source_root = args.challenge_file.resolve().parent if args.challenge_file else Path.cwd()
    challenge_name = args.challenge_name or challenge.get("challenge_name")
    challenge_text = args.challenge_text or challenge.get("challenge_text")
    category_hint = args.category_hint or challenge.get("category_hint")
    artifact_paths = list(challenge.get("artifact_paths", [])) + args.artifact
    target_host = challenge.get("target_host")
    challenge_metadata = dict(challenge.get("challenge_metadata", {}))

    challenge_payload = {
        "challenge_name": challenge_name,
        "challenge_text": challenge_text,
        "category_hint": category_hint,
        "target_host": target_host,
        "challenge_metadata": challenge_metadata,
        "artifact_paths": artifact_paths,
    }
    try:
        discord_config = resolve_discord_config(
            bot_token=args.discord_bot_token,
            parent_channel_id=args.discord_parent_channel_id,
            auto_archive_duration=args.discord_auto_archive_duration,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    dispatcher = None
    observer = None
    if discord_config:
        client = DiscordClient(discord_config)
        dispatcher = DiscordDispatcher(client)
        observer = ChallengeDiscordObserver(client, dispatcher)

    backend_sequence = [item.strip() for item in args.backend_sequence.split(",") if item.strip()]
    try:
        run_request = ChallengeRunRequest(
            challenge_payload=challenge_payload,
            source_root=source_root,
            workspace_root=args.workspace,
            skills_root=args.skills_root,
            thread_id=args.thread_id,
            max_attempts=args.max_attempts,
            backend_sequence=backend_sequence,
        )
        if args.parallel_trajectories and args.parallel_trajectories > 1:
            from .orchestrator_service import run_challenge_parallel

            result = run_challenge_parallel(
                run_request,
                n=int(args.parallel_trajectories),
                event_sink=observer.handle_event if observer is not None else None,
            )
        else:
            result = run_challenge(
                run_request,
                event_sink=observer.handle_event if observer is not None else None,
            )
    finally:
        if dispatcher is not None:
            dispatcher.close()
    print(json.dumps(result.final_state, indent=2))
    return 0


def _load_challenge_file(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_env_file_arg(argv: list[str]) -> Path | None:
    default_env_file = Path(".env")
    if "--env-file" in argv:
        index = argv.index("--env-file")
        if index + 1 >= len(argv):
            raise SystemExit("--env-file requires a path value.")
        return Path(argv[index + 1]).expanduser().resolve()

    for item in argv:
        if item.startswith("--env-file="):
            _, value = item.split("=", 1)
            if not value:
                raise SystemExit("--env-file requires a path value.")
            return Path(value).expanduser().resolve()

    if default_env_file.exists():
        return default_env_file.resolve()
    return None


def _load_env_file(path: Path | None) -> None:
    if path is None:
        return
    if not path.exists():
        raise SystemExit(f"env file not found: {path}")

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, _parse_env_value(value.strip()))


def _parse_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


if __name__ == "__main__":
    sys.exit(main())
