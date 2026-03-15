from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable

from .challenges import normalize_challenge_payload
from .graph import build_initial_state, build_orchestrator, load_resume_context
from .writeups import generate_writeup_markdown
from .workers import build_worker_pool
from .workspace import prepare_challenge_workspace


EventSink = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class ChallengeRunRequest:
    challenge_payload: dict[str, Any]
    backend_sequence: list[str]
    max_attempts: int
    skills_root: Path
    workspace_root: Path
    artifact_cookie_header: str | None = None
    thread_id: str = "ctf-poc"
    source_root: Path | None = None


@dataclass(frozen=True)
class ChallengeRunResult:
    challenge_name: str
    workspace: Path
    staged_artifacts: list[str]
    final_state: dict[str, Any]


def run_challenge(
    request: ChallengeRunRequest,
    event_sink: EventSink | None = None,
) -> ChallengeRunResult:
    challenge = normalize_challenge_payload(dict(request.challenge_payload))
    challenge_name = challenge.get("challenge_name")
    challenge_text = challenge.get("challenge_text")
    category_hint = challenge.get("category_hint")
    artifact_paths = list(challenge.get("artifact_paths", []))
    target_host = challenge.get("target_host")
    challenge_metadata = dict(challenge.get("challenge_metadata", {}))

    if not challenge_name or not challenge_text:
        raise SystemExit("challenge name and challenge text are required.")
    validate_challenge_actionability(str(challenge_name), target_host, challenge_metadata)

    challenge_workspace, staged_artifacts = prepare_challenge_workspace(
        workspace_root=request.workspace_root,
        challenge_name=str(challenge_name),
        artifact_paths=artifact_paths,
        challenge_payload=challenge,
        source_root=request.source_root or Path.cwd(),
        artifact_cookie_header=request.artifact_cookie_header,
    )
    _emit(
        event_sink,
        "challenge_workspace_prepared",
        {
            "challenge_name": str(challenge_name),
            "challenge_text": str(challenge_text),
            "category_hint": category_hint,
            "target_host": target_host,
            "challenge_metadata": challenge_metadata,
            "artifact_paths": list(staged_artifacts),
            "workspace": str(challenge_workspace),
        },
    )

    workers = build_worker_pool(request.backend_sequence)
    graph = build_orchestrator(
        request.skills_root,
        workers,
        event_handler=_wrap_challenge_event_sink(
            event_sink,
            challenge_name=str(challenge_name),
            workspace=challenge_workspace,
        ),
    )
    resumed_history, resumed_memory = load_resume_context(challenge_workspace)
    if resumed_history:
        _emit(
            event_sink,
            "challenge_resume_loaded",
            {
                "challenge_name": str(challenge_name),
                "workspace": str(challenge_workspace),
                "prior_attempts": len(resumed_history),
            },
        )
    initial_state = build_initial_state(
        challenge_name=str(challenge_name),
        challenge_text=str(challenge_text),
        workspace=challenge_workspace,
        backend_sequence=request.backend_sequence,
        category_hint=category_hint if isinstance(category_hint, str) else None,
        target_host=target_host if isinstance(target_host, str) else None,
        challenge_metadata=challenge_metadata,
        artifact_paths=staged_artifacts,
        history=resumed_history,
        working_memory=resumed_memory,
        max_attempts=request.max_attempts,
    )
    final_state = graph.invoke(
        initial_state,
        config={"configurable": {"thread_id": request.thread_id}},
    )
    maybe_write_writeup(
        workspace=challenge_workspace,
        challenge_name=str(challenge_name),
        challenge_text=str(challenge_text),
        category_hint=category_hint if isinstance(category_hint, str) else None,
        target_host=target_host if isinstance(target_host, str) else None,
        final_state=final_state,
        skills_root=request.skills_root,
        workers=workers,
        backend_sequence=request.backend_sequence,
    )
    final_state["workspace"] = str(challenge_workspace)
    _emit(
        event_sink,
        "challenge_completed",
        {
            "challenge_name": str(challenge_name),
            "workspace": str(challenge_workspace),
            **final_state,
        },
    )
    return ChallengeRunResult(
        challenge_name=str(challenge_name),
        workspace=challenge_workspace,
        staged_artifacts=list(staged_artifacts),
        final_state=final_state,
    )


def validate_challenge_actionability(
    challenge_name: str,
    target_host: str | None,
    challenge_metadata: dict[str, Any],
) -> None:
    import_metadata = challenge_metadata.get("import_metadata")
    if not isinstance(import_metadata, dict):
        return
    if target_host:
        return
    if not import_metadata.get("start_instance_requested"):
        return

    start_result = str(import_metadata.get("start_instance_result") or "unknown")
    warnings = import_metadata.get("warnings")
    detail_suffix = ""
    if isinstance(warnings, list) and warnings:
        detail_suffix = f" Details: {'; '.join(str(item) for item in warnings)}"
    raise SystemExit(
        f"Challenge '{challenge_name}' is not actionable: instance access is missing "
        f"after requested startup (start_instance_result={start_result}).{detail_suffix}"
    )


def maybe_write_writeup(
    workspace: Path,
    challenge_name: str,
    challenge_text: str,
    category_hint: str | None,
    target_host: str | None,
    final_state: dict[str, Any],
    skills_root: Path | None = None,
    workers: dict[str, object] | None = None,
    backend_sequence: list[str] | None = None,
) -> None:
    if not final_state.get("solved"):
        return
    markdown = None
    if skills_root is not None and workers and backend_sequence:
        try:
            markdown = generate_writeup_markdown(
                workspace=workspace,
                skills_root=skills_root,
                workers=workers,
                backend_sequence=backend_sequence,
                challenge_name=challenge_name,
                challenge_text=challenge_text,
                category_hint=category_hint,
                target_host=target_host,
                final_state=final_state,
            )
        except Exception:
            markdown = None

    try:
        writeup_path = workspace / "writeup.md"
        writeup_path.write_text(
            markdown
            or render_writeup_markdown(
                challenge_name=challenge_name,
                challenge_text=challenge_text,
                category_hint=category_hint,
                target_host=target_host,
                final_state=final_state,
            ),
            encoding="utf-8",
        )
    except Exception:
        return


def render_writeup_markdown(
    challenge_name: str,
    challenge_text: str,
    category_hint: str | None,
    target_host: str | None,
    final_state: dict[str, Any],
) -> str:
    history = [item for item in final_state.get("history", []) if isinstance(item, dict)]
    latest_output = final_state.get("latest_worker_output", {})
    latest_commands = list(latest_output.get("commands", [])) if isinstance(latest_output, dict) else []
    flag = str(final_state.get("final_flag") or "").strip()
    summary = _compact_text(str(final_state.get("final_summary", "")))
    approach_points = _build_writeup_approach_points(summary, history)
    commands = _collect_writeup_commands(history, latest_commands)
    script_snippets = _collect_writeup_scripts(history)

    lines = [
        "# Writeup",
        "",
        f"**Challenge:** {challenge_name}",
    ]
    if category_hint:
        lines.append(f"**Category:** `{category_hint}`")
    if target_host:
        lines.append(f"**Target:** `{target_host}`")
    if flag:
        lines.append(f"**Flag:** `{flag}`")

    lines.extend(
        [
            "",
            "## Challenge",
            "",
            _compact_text(challenge_text, limit=600),
            "",
            "## Resolution",
            "",
        ]
    )
    lines.extend(f"- {point}" for point in approach_points)

    lines.extend(
        [
            "",
            "## Solve",
            "",
        ]
    )
    if commands:
        lines.extend(
            [
                "```bash",
                *commands,
                "```",
            ]
        )
    else:
        lines.append("No shell commands were required to recover the flag.")

    if script_snippets:
        lines.extend(
            [
                "",
                "## Scripts",
                "",
            ]
        )
        for index, snippet in enumerate(script_snippets, 1):
            language = _guess_script_language(snippet)
            lines.extend(
                [
                    f"### Script {index}",
                    "",
                    f"```{language}",
                    snippet,
                    "```",
                    "",
                ]
            )
        if lines[-1] == "":
            lines.pop()

    return "\n".join(lines).strip() + "\n"


def _emit(event_sink: EventSink | None, event_type: str, payload: dict[str, Any]) -> None:
    if event_sink is None:
        return
    event_sink(event_type, payload)


def _wrap_challenge_event_sink(
    event_sink: EventSink | None,
    *,
    challenge_name: str,
    workspace: Path,
) -> EventSink | None:
    if event_sink is None:
        return None

    def handler(event_type: str, payload: dict[str, Any]) -> None:
        event_sink(
            event_type,
            {
                "challenge_name": challenge_name,
                "workspace": str(workspace),
                **payload,
            },
        )

    return handler


def _build_writeup_approach_points(summary: str, history: list[dict[str, Any]]) -> list[str]:
    points: list[str] = []
    if summary:
        points.append(summary)

    for attempt in history[-3:]:
        attempt_summary = _compact_text(str(attempt.get("summary", "")), limit=220)
        if attempt_summary and attempt_summary not in points:
            points.append(attempt_summary)
        for evidence in attempt.get("evidence", []):
            compact = _compact_text(str(evidence), limit=180)
            if compact and compact not in points:
                points.append(compact)
            if len(points) >= 5:
                return points
    return points[:5] or ["The challenge was solved and the final flag was validated by the worker."]


def _collect_writeup_commands(history: list[dict[str, Any]], latest_commands: list[str]) -> list[str]:
    commands: list[str] = []
    seen: set[str] = set()
    for command in latest_commands:
        compact = _compact_text(str(command), limit=500)
        if compact and compact not in seen:
            seen.add(compact)
            commands.append(compact)
    for attempt in reversed(history):
        for command in attempt.get("key_commands", []):
            compact = _compact_text(str(command), limit=500)
            if compact and compact not in seen:
                seen.add(compact)
                commands.append(compact)
            if len(commands) >= 8:
                return commands
    return commands


def _collect_writeup_scripts(history: list[dict[str, Any]]) -> list[str]:
    snippets: list[str] = []
    seen: set[str] = set()
    for attempt in reversed(history):
        for item in attempt.get("inline_scripts", []):
            if not isinstance(item, dict):
                continue
            snippet = _compact_text(str(item.get("snippet", "")), limit=1200, preserve_newlines=True)
            if not snippet or snippet in seen:
                continue
            seen.add(snippet)
            snippets.append(snippet)
            if len(snippets) >= 3:
                return snippets
    return snippets


def _compact_text(value: str, limit: int = 320, preserve_newlines: bool = False) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    if preserve_newlines:
        lines = [" ".join(line.split()) for line in normalized.splitlines()]
        compact = "\n".join(line for line in lines if line)
    else:
        compact = " ".join(normalized.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3].rstrip()}..."


def _guess_script_language(snippet: str) -> str:
    lowered = snippet.lstrip().lower()
    if lowered.startswith("import ") or lowered.startswith("from "):
        return "python"
    if lowered.startswith("#!/usr/bin/env python") or lowered.startswith("#!/usr/bin/python"):
        return "python"
    return "text"
