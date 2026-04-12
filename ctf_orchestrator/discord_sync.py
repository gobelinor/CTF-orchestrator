from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import threading
import time
import sys
from typing import Any, Callable, Protocol
from urllib import error, request

from .workspace import merge_challenge_manifest


DISCORD_API_BASE_URL = "https://discord.com/api/v10"
MAX_THREAD_NAME_LENGTH = 100
MAX_MESSAGE_LENGTH = 1900
DEFAULT_WORKER_COMMAND_BATCH_SECONDS = 10.0
THREAD_TYPE_BY_NAME = {
    "public": 11,
    "private": 12,
}


@dataclass(frozen=True)
class DiscordConfig:
    bot_token: str
    parent_channel_id: str
    auto_archive_duration: int = 10080


@dataclass(frozen=True)
class DiscordThreadRef:
    thread_id: str
    thread_name: str
    parent_channel_id: str


class DiscordTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...


class DiscordApiError(RuntimeError):
    pass


class DiscordHttpTransport:
    def __init__(
        self,
        bot_token: str,
        *,
        max_retries: int = 3,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.bot_token = bot_token
        self.max_retries = max_retries
        self.sleep_fn = sleep_fn

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            body = None if payload is None else json.dumps(payload).encode("utf-8")
            headers = {
                "Authorization": f"Bot {self.bot_token}",
                "User-Agent": "ctf-orchestrator/0.1",
            }
            if body is not None:
                headers["Content-Type"] = "application/json"

            req = request.Request(
                f"{DISCORD_API_BASE_URL}{path}",
                data=body,
                headers=headers,
                method=method,
            )
            try:
                with request.urlopen(req) as response:
                    text = response.read().decode("utf-8")
            except error.HTTPError as exc:
                try:
                    error_text = exc.read().decode("utf-8", errors="replace")
                finally:
                    exc.close()
                if exc.code == 429 and attempt < self.max_retries:
                    retry_after = _extract_retry_after_seconds(error_text, exc.headers)
                    self.sleep_fn(retry_after)
                    continue
                raise DiscordApiError(f"Discord API returned HTTP {exc.code}: {error_text}") from exc
            except error.URLError as exc:
                raise DiscordApiError(f"Unable to reach Discord API: {exc.reason}") from exc

            if not text:
                return {}
            payload_data = json.loads(text)
            if not isinstance(payload_data, dict):
                raise DiscordApiError("Discord API returned an unexpected payload.")
            return payload_data
        raise DiscordApiError("Discord API retry budget exhausted.")


class TimerHandle(Protocol):
    def start(self) -> None:
        ...

    def cancel(self) -> None:
        ...


@dataclass(frozen=True)
class WorkerCommandEvent:
    status: str
    backend: str
    command: str
    exit_code: Any = None


def _default_timer_factory(interval_seconds: float, callback: Callable[[], None]) -> TimerHandle:
    timer = threading.Timer(interval_seconds, callback)
    timer.daemon = True
    return timer


class DiscordClient:
    def __init__(
        self,
        config: DiscordConfig,
        transport: DiscordTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or DiscordHttpTransport(config.bot_token)
        self._request_lock = threading.Lock()

    def ensure_challenge_thread(
        self,
        workspace: Path,
        challenge_name: str,
        challenge_text: str,
        category_hint: str | None,
        target_host: str | None,
        challenge_metadata: dict[str, Any] | None = None,
        artifact_paths: list[str] | None = None,
    ) -> DiscordThreadRef:
        existing = load_thread_binding(workspace)
        if existing is not None:
            return existing

        thread_name = _normalize_thread_name(challenge_name)
        challenge_excerpt = _truncate(challenge_text.strip(), 900)
        artifacts = list(artifact_paths or [])
        metadata = dict(challenge_metadata or {})
        initial_message = _render_initial_message(
            challenge_name=challenge_name,
            challenge_excerpt=challenge_excerpt,
            workspace=workspace,
            category_hint=category_hint,
            target_host=target_host,
            artifact_paths=artifacts,
            challenge_metadata=metadata,
        )

        created = self._request(
            "POST",
            f"/channels/{self.config.parent_channel_id}/threads",
            payload={
                "name": thread_name,
                "auto_archive_duration": self.config.auto_archive_duration,
                "type": THREAD_TYPE_BY_NAME["public"],
            },
        )
        thread_id = str(created["id"])
        self.post_message(thread_id, initial_message)

        binding = DiscordThreadRef(
            thread_id=thread_id,
            thread_name=str(created.get("name", thread_name)),
            parent_channel_id=self.config.parent_channel_id,
        )
        save_thread_binding(workspace, binding)
        return binding

    def publish_route(
        self,
        thread: DiscordThreadRef,
        category: str,
        reason: str,
        skill_slug: str,
    ) -> None:
        content = "\n".join(
            [
                "Routing completed.",
                f"Category: `{category}`",
                f"Skill: `{skill_slug}`",
                f"Reason: {_truncate(reason, 600)}",
            ]
        )
        self.post_message(thread.thread_id, content)

    def publish_attempt(self, thread: DiscordThreadRef, attempt: dict[str, Any]) -> None:
        lines = [
            f"Attempt {attempt['attempt']} via `{attempt['backend']}`",
            f"Status: `{attempt['status']}`",
            f"Summary: {_truncate(str(attempt.get('summary', '')), 700)}",
            f"Next step: {_truncate(str(attempt.get('next_step', '')), 500)}",
        ]
        flag = attempt.get("flag")
        if flag:
            lines.append(f"Flag candidate: `{flag}`")
        commands = list(attempt.get("commands", []))[:5]
        if commands:
            lines.append("Commands:")
            lines.extend(f"- `{_truncate(command, 180)}`" for command in commands)
        self.post_message(thread.thread_id, "\n".join(lines))

    def publish_final(self, thread: DiscordThreadRef, final_state: dict[str, Any]) -> None:
        lines = [
            "Run completed.",
            f"Solved: `{'yes' if final_state.get('solved') else 'no'}`",
            f"Stop reason: `{final_state.get('stop_reason', 'unknown')}`",
            f"Attempts: `{final_state.get('attempts', 0)}`",
            f"Summary: {_truncate(str(final_state.get('final_summary', '')), 700)}",
        ]
        final_flag = final_state.get("final_flag")
        if final_flag:
            lines.append(f"Final flag: `{final_flag}`")
        self.post_message(thread.thread_id, "\n".join(lines))
        writeup_content = _load_writeup_content(final_state)
        if writeup_content:
            self.post_message(thread.thread_id, writeup_content)

    def post_message(self, channel_id: str, content: str) -> None:
        for chunk in _chunk_message(content):
            self._request(
                "POST",
                f"/channels/{channel_id}/messages",
                payload={"content": chunk},
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._request_lock:
            return self.transport.request(method, path, payload=payload)


class DiscordDispatcher:
    def __init__(
        self,
        client: DiscordClient,
        *,
        flush_interval_seconds: float = DEFAULT_WORKER_COMMAND_BATCH_SECONDS,
        timer_factory: Callable[[float, Callable[[], None]], TimerHandle] = _default_timer_factory,
    ) -> None:
        self.client = client
        self.flush_interval_seconds = flush_interval_seconds
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._pending_commands: dict[str, list[WorkerCommandEvent]] = {}
        self._timers: dict[str, TimerHandle] = {}
        self._closed = False

    def enqueue_worker_command(
        self,
        channel_id: str,
        *,
        status: str,
        backend: str,
        command: str,
        exit_code: Any = None,
    ) -> None:
        timer_to_start: TimerHandle | None = None
        with self._lock:
            if self._closed:
                return
            self._pending_commands.setdefault(channel_id, []).append(
                WorkerCommandEvent(
                    status=status,
                    backend=backend,
                    command=command,
                    exit_code=exit_code,
                )
            )
            if channel_id not in self._timers:
                timer = self._timer_factory(
                    self.flush_interval_seconds,
                    lambda: self._flush_channel_commands_safe(channel_id),
                )
                self._timers[channel_id] = timer
                timer_to_start = timer
        if timer_to_start is not None:
            timer_to_start.start()

    def flush_channel_commands(self, channel_id: str) -> None:
        events = self._take_channel_events(channel_id)
        if not events:
            return
        try:
            self.client.post_message(
                channel_id,
                _render_worker_command_batch(events, self.flush_interval_seconds),
            )
        except Exception:
            self._requeue_channel_events(channel_id, events)
            raise

    def close(self) -> None:
        with self._lock:
            self._closed = True
            channel_ids = list(self._pending_commands.keys())
        for channel_id in channel_ids:
            try:
                self.flush_channel_commands(channel_id)
            except Exception as exc:
                sys.stderr.write(f"[warning] discord dispatcher flush failed for {channel_id}: {exc}\n")
                sys.stderr.flush()

    def _take_channel_events(self, channel_id: str) -> list[WorkerCommandEvent]:
        with self._lock:
            events = list(self._pending_commands.pop(channel_id, []))
            timer = self._timers.pop(channel_id, None)
        if timer is not None:
            timer.cancel()
        return events

    def _requeue_channel_events(self, channel_id: str, events: list[WorkerCommandEvent]) -> None:
        if not events:
            return
        timer_to_start: TimerHandle | None = None
        with self._lock:
            if self._closed:
                return
            current = self._pending_commands.setdefault(channel_id, [])
            self._pending_commands[channel_id] = list(events) + current
            if channel_id not in self._timers:
                timer = self._timer_factory(
                    self.flush_interval_seconds,
                    lambda: self._flush_channel_commands_safe(channel_id),
                )
                self._timers[channel_id] = timer
                timer_to_start = timer
        if timer_to_start is not None:
            timer_to_start.start()

    def _flush_channel_commands_safe(self, channel_id: str) -> None:
        try:
            self.flush_channel_commands(channel_id)
        except Exception as exc:
            sys.stderr.write(f"[warning] discord dispatcher flush failed for {channel_id}: {exc}\n")
            sys.stderr.flush()


def load_thread_binding(workspace: Path) -> DiscordThreadRef | None:
    state_path = workspace / ".discord-thread.json"
    if not state_path.exists():
        return None
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    return DiscordThreadRef(
        thread_id=str(payload["thread_id"]),
        thread_name=str(payload["thread_name"]),
        parent_channel_id=str(payload["parent_channel_id"]),
    )


def resolve_discord_config(
    *,
    bot_token: str | None,
    parent_channel_id: str | None,
    auto_archive_duration: int = 10080,
) -> DiscordConfig | None:
    token = (bot_token or "").strip()
    channel_id = (parent_channel_id or "").strip()
    if not token and not channel_id:
        return None
    if not token or not channel_id:
        raise ValueError("Discord integration requires both a bot token and a parent channel ID.")
    if auto_archive_duration not in {60, 1440, 4320, 10080}:
        raise ValueError("discord auto archive duration must be one of 60, 1440, 4320 or 10080.")

    return DiscordConfig(
        bot_token=token,
        parent_channel_id=channel_id,
        auto_archive_duration=auto_archive_duration,
    )


def save_thread_binding(workspace: Path, binding: DiscordThreadRef) -> None:
    state_path = workspace / ".discord-thread.json"
    state_path.write_text(
        json.dumps(asdict(binding), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    merge_challenge_manifest(
        workspace,
        {
            "discord_thread": {
                "thread_id": binding.thread_id,
                "thread_name": binding.thread_name,
                "parent_channel_id": binding.parent_channel_id,
            }
        },
    )


class ChallengeDiscordObserver:
    def __init__(
        self,
        client: DiscordClient,
        dispatcher: DiscordDispatcher | None = None,
    ) -> None:
        self.client = client
        self.dispatcher = dispatcher or DiscordDispatcher(client)
        self._threads_by_workspace: dict[str, DiscordThreadRef] = {}

    def handle_event(self, event_type: str, payload: dict[str, Any]) -> None:
        workspace = payload.get("workspace")
        if not workspace:
            return
        workspace_path = Path(str(workspace))

        if event_type == "challenge_workspace_prepared":
            self._handle_workspace_prepared(workspace_path, payload)
            return

        thread = self._get_thread(workspace_path)
        if thread is None:
            return

        handler = self._EVENT_HANDLERS.get(event_type)
        if handler is None:
            # Unknown event: log a debug line instead of silently dropping.
            try:
                self.client.post_message(
                    thread.thread_id,
                    f"*event* {event_type} (no handler registered)",
                )
            except Exception:
                pass
            return
        handler(self, thread, payload)

    def _handle_workspace_prepared(self, workspace_path: Path, payload: dict[str, Any]) -> None:
        thread = self.client.ensure_challenge_thread(
            workspace=workspace_path,
            challenge_name=str(payload.get("challenge_name", "challenge")),
            challenge_text=str(payload.get("challenge_text", "")),
            category_hint=_maybe_str(payload.get("category_hint")),
            target_host=_maybe_str(payload.get("target_host")),
            challenge_metadata=_maybe_dict(payload.get("challenge_metadata")),
            artifact_paths=[str(item) for item in list(payload.get("artifact_paths", []))],
        )
        self._threads_by_workspace[str(workspace_path)] = thread

    def _handle_route_resolved(self, thread: "DiscordThreadRef", payload: dict[str, Any]) -> None:
        self.client.publish_route(
            thread,
            category=str(payload.get("category", "unknown")),
            reason=str(payload.get("category_reason", "")),
            skill_slug=str(payload.get("specialist_skill_slug", "")),
        )

    def _handle_attempt_completed(self, thread: "DiscordThreadRef", payload: dict[str, Any]) -> None:
        self.dispatcher.flush_channel_commands(thread.thread_id)
        self.client.publish_attempt(thread, payload)

    def _handle_attempt_analyzed(self, thread: "DiscordThreadRef", payload: dict[str, Any]) -> None:
        confidence = payload.get("worker_confidence")
        failure_reason = payload.get("worker_failure_reason")
        stagnation = payload.get("stagnation_signals") or []
        self.client.post_message(
            thread.thread_id,
            (
                "*analyze* "
                f"confidence={confidence} "
                f"failure={failure_reason} "
                f"stagnation={','.join(stagnation) if stagnation else 'none'}"
            ),
        )

    def _handle_decision_made(self, thread: "DiscordThreadRef", payload: dict[str, Any]) -> None:
        self.client.post_message(
            thread.thread_id,
            (
                "*decide* "
                f"{payload.get('decision', 'unknown')} "
                f"({payload.get('reason', '')}) "
                f"stop_reason={payload.get('stop_reason', '')}"
            ),
        )

    def _handle_raw_output_summarized(self, thread: "DiscordThreadRef", payload: dict[str, Any]) -> None:
        self.client.post_message(
            thread.thread_id,
            f"*summarizer* compressed attempt {payload.get('attempt')} → {payload.get('compressed_length')} chars",
        )

    def _handle_memory_persist_completed(self, thread: "DiscordThreadRef", payload: dict[str, Any]) -> None:
        persisted = payload.get("persisted")
        skipped = payload.get("skipped_reason")
        self.client.post_message(
            thread.thread_id,
            f"*memory* persisted={persisted} skipped={skipped or 'n/a'}",
        )

    def _handle_memory_persist_failed(self, thread: "DiscordThreadRef", payload: dict[str, Any]) -> None:
        self.client.post_message(
            thread.thread_id,
            f"*memory* failed: {payload.get('error', '')}",
        )

    def _handle_supervisor_agent_decision(self, thread: "DiscordThreadRef", payload: dict[str, Any]) -> None:
        self.client.post_message(
            thread.thread_id,
            (
                "*supervisor* "
                f"{payload.get('decision', 'unknown')} "
                f"({payload.get('reason', '')}) "
                f"notes={payload.get('notes', '')}"
            ),
        )

    def _handle_trajectory_solved(self, thread: "DiscordThreadRef", payload: dict[str, Any]) -> None:
        self.client.post_message(
            thread.thread_id,
            f"*trajectory {payload.get('trajectory')}* solved flag={payload.get('final_flag')}",
        )

    def _handle_worker_command_started(self, thread: "DiscordThreadRef", payload: dict[str, Any]) -> None:
        self.dispatcher.enqueue_worker_command(
            thread.thread_id,
            status="started",
            backend=str(payload.get("backend", "worker")),
            command=str(payload.get("command", "")),
        )

    def _handle_worker_command_completed(self, thread: "DiscordThreadRef", payload: dict[str, Any]) -> None:
        self.dispatcher.enqueue_worker_command(
            thread.thread_id,
            status="completed",
            backend=str(payload.get("backend", "worker")),
            command=str(payload.get("command", "")),
            exit_code=payload.get("exit_code"),
        )

    def _handle_challenge_completed(self, thread: "DiscordThreadRef", payload: dict[str, Any]) -> None:
        self.dispatcher.flush_channel_commands(thread.thread_id)
        self.client.publish_final(thread, payload)

    _EVENT_HANDLERS: dict[str, Any] = {
        "route_resolved": _handle_route_resolved,
        "attempt_completed": _handle_attempt_completed,
        "attempt_analyzed": _handle_attempt_analyzed,
        "decision_made": _handle_decision_made,
        "raw_output_summarized": _handle_raw_output_summarized,
        "memory_persist_completed": _handle_memory_persist_completed,
        "memory_persist_failed": _handle_memory_persist_failed,
        "supervisor_agent_decision": _handle_supervisor_agent_decision,
        "trajectory_solved": _handle_trajectory_solved,
        "worker_command_started": _handle_worker_command_started,
        "worker_command_completed": _handle_worker_command_completed,
        "challenge_completed": _handle_challenge_completed,
    }

    def _get_thread(self, workspace: Path) -> DiscordThreadRef | None:
        existing = self._threads_by_workspace.get(str(workspace))
        if existing is not None:
            return existing
        existing = load_thread_binding(workspace)
        if existing is not None:
            self._threads_by_workspace[str(workspace)] = existing
        return existing


class CampaignDiscordObserver:
    def __init__(self, client: DiscordClient, campaign_dir: Path) -> None:
        self.client = client
        self.campaign_dir = campaign_dir

    def handle_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "campaign_started":
            remove_campaign_thread_binding(self.campaign_dir)
            self.client.post_message(
                self.client.config.parent_channel_id,
                _render_campaign_started_message(payload),
            )
            return
        if event_type == "campaign_import_completed":
            self.client.post_message(self.client.config.parent_channel_id, _render_campaign_import_message(payload))
            return
        if event_type == "campaign_challenge_started":
            self.client.post_message(self.client.config.parent_channel_id, _render_campaign_challenge_started(payload))
            return
        if event_type == "campaign_challenge_completed":
            self.client.post_message(self.client.config.parent_channel_id, _render_campaign_challenge_completed(payload))
            return
        if event_type == "campaign_completed":
            self.client.post_message(self.client.config.parent_channel_id, _render_campaign_completed(payload))


def _chunk_message(content: str) -> list[str]:
    stripped = content.strip()
    if not stripped:
        return []

    chunks: list[str] = []
    current = ""
    for line in stripped.splitlines():
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= MAX_MESSAGE_LENGTH:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > MAX_MESSAGE_LENGTH:
            chunks.append(line[:MAX_MESSAGE_LENGTH])
            line = line[MAX_MESSAGE_LENGTH:]
        current = line
    if current:
        chunks.append(current)
    return chunks


def _normalize_thread_name(value: str) -> str:
    name = " ".join(value.split()).strip()
    if not name:
        name = "challenge"
    return name[:MAX_THREAD_NAME_LENGTH]


def _render_initial_message(
    *,
    challenge_name: str,
    challenge_excerpt: str,
    workspace: Path,
    category_hint: str | None,
    target_host: str | None,
    artifact_paths: list[str],
    challenge_metadata: dict[str, Any],
) -> str:
    lines = [
        f"Challenge: **{challenge_name}**",
        f"Workspace: `{workspace}`",
        f"Category hint: `{category_hint or 'none'}`",
        f"Target host: `{target_host or 'none'}`",
    ]
    if artifact_paths:
        lines.append("Artifacts:")
        lines.extend(f"- `{path}`" for path in artifact_paths[:10])
    if challenge_metadata:
        metadata_summary = ", ".join(
            f"{key}={value}" for key, value in list(challenge_metadata.items())[:5]
        )
        lines.append(f"Metadata: `{_truncate(metadata_summary, 300)}`")
    lines.append("Challenge text:")
    lines.append(f"```text\n{challenge_excerpt}\n```")
    return "\n".join(lines)


def _truncate(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3].rstrip()}..."


def _extract_retry_after_seconds(error_text: str, headers: Any) -> float:
    retry_after = None
    if isinstance(headers, dict):
        retry_after = headers.get("Retry-After")
    elif headers is not None:
        retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    try:
        payload = json.loads(error_text)
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        try:
            return max(0.0, float(payload.get("retry_after", 1.0)))
        except (TypeError, ValueError):
            return 1.0
    return 1.0


def _render_worker_command_batch(
    events: list[WorkerCommandEvent],
    flush_interval_seconds: float,
) -> str:
    lines = [f"Worker activity ({_format_window_seconds(flush_interval_seconds)} window)."]
    for event in events:
        status_line = f"[{event.backend}] {event.status}"
        if event.exit_code is not None:
            status_line += f" (exit {event.exit_code})"
        lines.append(f"- {status_line}: `{_truncate(event.command, 500)}`")
    return "\n".join(lines)


def _format_window_seconds(value: float) -> str:
    rounded = int(value)
    if abs(value - rounded) < 1e-9:
        return f"{rounded}s"
    return f"{value:.1f}s"


def _load_writeup_content(final_state: dict[str, Any]) -> str | None:
    if not final_state.get("solved"):
        return None

    workspace = final_state.get("workspace")
    if not workspace:
        return None

    writeup_path = Path(str(workspace)) / "writeup.md"
    if not writeup_path.exists():
        return None

    content = writeup_path.read_text(encoding="utf-8").strip()
    if not content:
        return None
    return content


def _maybe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _maybe_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    return None


def _render_campaign_started_message(payload: dict[str, Any]) -> str:
    lines = [
        f"Campaign: **{payload.get('campaign_name', 'campaign')}**",
        f"Source: `{payload.get('source_label', 'unknown')}`",
    ]
    filters = payload.get("filters")
    if isinstance(filters, dict) and filters:
        lines.append(f"Filters: `{_truncate(json.dumps(filters, ensure_ascii=False), 500)}`")
    capacities = payload.get("capacities")
    if isinstance(capacities, dict) and capacities:
        lines.append(f"Capacities: `{_truncate(json.dumps(capacities, ensure_ascii=False), 300)}`")
    return "\n".join(lines)


def _render_campaign_import_message(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Campaign import completed.",
            f"Discovered: `{payload.get('discovered', 0)}`",
            f"Eligible: `{payload.get('eligible', 0)}`",
            f"Skipped: `{payload.get('skipped', 0)}`",
            f"Import failed: `{payload.get('import_failed', 0)}`",
        ]
    )


def _render_campaign_challenge_started(payload: dict[str, Any]) -> str:
    lines = [
        f"Starting challenge: **{payload.get('challenge_name', 'challenge')}**",
        f"Category: `{payload.get('category', 'unknown')}`",
        f"Priority: `{payload.get('priority_reason', 'n/a')}`",
    ]
    if payload.get("instance_required"):
        lines.append("Consumes instance capacity: `yes`")
    return "\n".join(lines)


def _render_campaign_challenge_completed(payload: dict[str, Any]) -> str:
    lines = [
        f"Challenge completed: **{payload.get('challenge_name', 'challenge')}**",
        f"Status: `{payload.get('status', 'unknown')}`",
        f"Summary: `{_truncate(str(payload.get('summary', '')), 500)}`",
    ]
    if payload.get("final_flag"):
        lines.append(f"Flag: `{payload.get('final_flag')}`")
    return "\n".join(lines)


def _render_campaign_completed(payload: dict[str, Any]) -> str:
    counts = payload.get("counts", {})
    return "\n".join(
        [
            "Campaign completed.",
            f"Solved: `{counts.get('solved', 0)}`",
            f"Needs human: `{counts.get('needs_human', 0)}`",
            f"Skipped: `{counts.get('skipped', 0)}`",
            f"Import failed: `{counts.get('import_failed', 0)}`",
            f"Interrupted: `{counts.get('interrupted', 0)}`",
        ]
    )


def remove_campaign_thread_binding(campaign_dir: Path) -> None:
    path = campaign_dir / ".discord-campaign-thread.json"
    if path.exists():
        path.unlink()
