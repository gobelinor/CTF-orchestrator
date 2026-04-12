from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Callable

from .challenges import normalize_challenge_payload
from .graph import build_initial_state, build_orchestrator, load_resume_context
from .memory import persist_challenge_memory
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
    writeup_markdown = maybe_write_writeup(
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
    maybe_persist_memory(
        workspace=challenge_workspace,
        challenge_name=str(challenge_name),
        challenge_text=str(challenge_text),
        category_hint=category_hint if isinstance(category_hint, str) else None,
        target_host=target_host if isinstance(target_host, str) else None,
        final_state=final_state,
        skills_root=request.skills_root,
        workers=workers,
        backend_sequence=request.backend_sequence,
        writeup_markdown=writeup_markdown,
        event_sink=event_sink,
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


def run_challenge_parallel(
    request: ChallengeRunRequest,
    *,
    n: int,
    event_sink: EventSink | None = None,
) -> ChallengeRunResult:
    """Spawn `n` isolated trajectories for the same challenge; first-flag-wins.

    Each trajectory uses a distinct thread_id and a distinct workspace suffix
    so their LangGraph checkpoint state, artifacts and .runs directory do not
    collide. As soon as one trajectory returns a solved final_state, the other
    trajectories are cancelled.
    """
    if n <= 1:
        return run_challenge(request, event_sink=event_sink)

    results: list[ChallengeRunResult] = []
    first_solved: ChallengeRunResult | None = None
    with ThreadPoolExecutor(max_workers=n) as executor:
        futures: dict[Future[Any], int] = {}
        for trajectory in range(n):
            thread_id = f"{request.thread_id}-t{trajectory}"
            payload = dict(request.challenge_payload)
            # Nudge the workspace slug so each trajectory gets its own folder.
            payload_name = str(payload.get("challenge_name") or payload.get("title") or "challenge")
            payload["challenge_name"] = f"{payload_name} [traj-{trajectory}]"
            trajectory_request = replace(
                request,
                thread_id=thread_id,
                challenge_payload=payload,
            )
            wrapped_sink = _wrap_trajectory_event_sink(event_sink, trajectory)
            future = executor.submit(run_challenge, trajectory_request, wrapped_sink)
            futures[future] = trajectory

        pending = set(futures.keys())
        while pending and first_solved is None:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    result = future.result()
                except Exception as exc:
                    _emit(
                        event_sink,
                        "trajectory_failed",
                        {"trajectory": futures[future], "error": str(exc)},
                    )
                    continue
                results.append(result)
                if result.final_state.get("solved") and first_solved is None:
                    first_solved = result
                    _emit(
                        event_sink,
                        "trajectory_solved",
                        {
                            "trajectory": futures[future],
                            "challenge_name": result.challenge_name,
                            "workspace": str(result.workspace),
                            "final_flag": result.final_state.get("final_flag"),
                        },
                    )
        # Best-effort cancel of still-pending futures.
        for future in pending:
            future.cancel()

    if first_solved is not None:
        return first_solved
    if results:
        # No trajectory solved; return the one with the most attempts (deepest exploration).
        return max(results, key=lambda r: int(r.final_state.get("attempts", 0) or 0))
    raise SystemExit("All parallel trajectories failed without a usable result.")


def _wrap_trajectory_event_sink(
    event_sink: EventSink | None, trajectory: int
) -> EventSink | None:
    if event_sink is None:
        return None

    def handler(event_type: str, payload: dict[str, Any]) -> None:
        event_sink(event_type, {"trajectory": trajectory, **payload})

    return handler


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
) -> str | None:
    if not final_state.get("solved"):
        return None
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

    final_markdown = markdown or render_writeup_markdown(
        challenge_name=challenge_name,
        challenge_text=challenge_text,
        category_hint=category_hint,
        target_host=target_host,
        final_state=final_state,
    )
    try:
        writeup_path = workspace / "writeup.md"
        writeup_path.write_text(final_markdown, encoding="utf-8")
    except Exception:
        return final_markdown
    return final_markdown


def maybe_persist_memory(
    workspace: Path,
    challenge_name: str,
    challenge_text: str,
    category_hint: str | None,
    target_host: str | None,
    final_state: dict[str, Any],
    skills_root: Path | None = None,
    workers: dict[str, Any] | None = None,
    backend_sequence: list[str] | None = None,
    writeup_markdown: str | None = None,
    event_sink: EventSink | None = None,
) -> dict[str, Any] | None:
    if skills_root is None or not workers or not backend_sequence:
        return None
    should_persist = bool(
        final_state.get("solved")
        or final_state.get("memory_persist_requested")
    )
    if not should_persist:
        return None
    try:
        result = persist_challenge_memory(
            workspace=workspace,
            skills_root=skills_root,
            workers=workers,
            backend_sequence=backend_sequence,
            challenge_name=challenge_name,
            challenge_text=challenge_text,
            category_hint=category_hint,
            target_host=target_host,
            final_state=final_state,
            writeup_markdown=writeup_markdown,
        )
    except Exception as exc:
        _emit(
            event_sink,
            "memory_persist_failed",
            {"challenge_name": challenge_name, "error": str(exc)},
        )
        return None
    if result is None:
        return None
    _emit(
        event_sink,
        "memory_persist_completed",
        {
            "challenge_name": challenge_name,
            "persisted": bool(result.get("persisted")),
            "group_id": result.get("group_id"),
            "episode_name": result.get("episode_name"),
            "skipped_reason": result.get("skipped_reason"),
        },
    )
    try:
        memory_log_path = workspace / ".runs" / "memory" / "result.json"
        memory_log_path.parent.mkdir(parents=True, exist_ok=True)
        memory_log_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return result


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
