from __future__ import annotations

import json
from pathlib import Path
import re
import warnings
from typing import Any, Callable, TypedDict

# Temporary local suppression until LangChain/LangGraph stop importing pydantic.v1 on Python 3.14+.
warnings.filterwarnings(
    "ignore",
    message=r"Core Pydantic V1 functionality isn't compatible with Python 3\.14 or greater\.",
    category=UserWarning,
    module=r"langchain_core\._api\.deprecation",
)

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .skills import Skill, load_skills, resolve_core_skill, resolve_specialist_skill, route_category
from .workers import WorkerRequest, WorkerResult, WorkerBackend, extract_flag


class ChallengeState(TypedDict, total=False):
    challenge_name: str
    challenge_text: str
    challenge_metadata: dict[str, Any]
    artifact_paths: list[str]
    category_hint: str | None
    target_host: str | None
    category: str
    category_reason: str
    core_skill_slug: str | None
    core_skill_path: str | None
    specialist_skill_slug: str
    specialist_skill_path: str
    backend_sequence: list[str]
    backend_index: int
    active_backend: str
    attempts: int
    max_attempts: int
    history: list[dict[str, Any]]
    latest_worker_output: dict[str, Any]
    working_memory: dict[str, Any]
    solved: bool
    final_flag: str | None
    final_summary: str
    stop_reason: str
    workspace: str


ATTEMPT_HISTORY_PATH = ".runs/attempt-history.json"
WORKING_MEMORY_PATH = ".runs/working-memory.json"


def build_orchestrator(
    skills_root: Path,
    workers: dict[str, WorkerBackend],
    event_handler: Callable[[str, dict[str, Any]], None] | None = None,
):
    skills = load_skills(skills_root)

    def route_node(state: ChallengeState) -> dict[str, Any]:
        category, reason = route_category(
            "\n".join(
                part
                for part in (
                    state["challenge_name"],
                    state["challenge_text"],
                    state.get("target_host", ""),
                )
                if part
            ),
            state.get("category_hint"),
        )
        core_skill = resolve_core_skill(skills)
        skill = resolve_specialist_skill(category, skills)
        payload = {
            "category": category,
            "category_reason": reason,
            "core_skill_slug": core_skill.slug if core_skill else None,
            "core_skill_path": str(core_skill.path) if core_skill else None,
            "specialist_skill_slug": skill.slug,
            "specialist_skill_path": str(skill.path),
            "backend_index": state.get("backend_index", 0),
            "attempts": state.get("attempts", 0),
            "history": list(state.get("history", [])),
            "working_memory": dict(state.get("working_memory", _empty_working_memory())),
            "solved": False,
            "final_flag": None,
            "final_summary": "",
            "stop_reason": "",
        }
        _emit_event(
            event_handler,
            "route_resolved",
            {
                "category": category,
                "category_reason": reason,
                "core_skill_slug": core_skill.slug if core_skill else None,
                "core_skill_path": str(core_skill.path) if core_skill else None,
                "specialist_skill_slug": skill.slug,
                "specialist_skill_path": str(skill.path),
            },
        )
        return payload

    def specialist_node(state: ChallengeState) -> dict[str, Any]:
        core_skill = _get_skill(skills, state["core_skill_slug"]) if state.get("core_skill_slug") else None
        skill = _get_skill(skills, state["specialist_skill_slug"])
        sequence = state["backend_sequence"]
        backend_index = state.get("backend_index", 0) % len(sequence)
        backend_name = sequence[backend_index]
        request = WorkerRequest(
            attempt_index=state.get("attempts", 0) + 1,
            challenge_name=state["challenge_name"],
            challenge_text=state["challenge_text"],
            challenge_category=state.get("category"),
            target_host=state.get("target_host"),
            metadata=dict(state.get("challenge_metadata", {})),
            artifact_paths=list(state.get("artifact_paths", [])),
            workspace=Path(state["workspace"]),
            skill=skill,
            prior_attempts=list(state.get("history", [])),
            working_memory=dict(state.get("working_memory", _empty_working_memory())),
            core_skill=core_skill,
        )
        result = workers[backend_name].invoke(
            request,
            event_sink=lambda worker_event_type, payload: _emit_event(event_handler, worker_event_type, payload),
        )
        history = list(state.get("history", []))
        attempt_record = _build_attempt_record(request, result)
        history.append(attempt_record)
        working_memory = _build_working_memory(Path(state["workspace"]), history, result)
        _persist_working_memory(Path(state["workspace"]), working_memory)
        _persist_attempt_history(Path(state["workspace"]), history)
        _emit_event(
            event_handler,
            "attempt_completed",
            dict(attempt_record),
        )
        return {
            "attempts": request.attempt_index,
            "active_backend": backend_name,
            "latest_worker_output": result.as_state_payload(),
            "history": history,
            "working_memory": working_memory,
        }

    def evaluator_node(state: ChallengeState) -> dict[str, Any]:
        latest = WorkerResult.from_payload(state["latest_worker_output"])
        flag = latest.flag or extract_flag(latest.summary) or extract_flag(latest.raw_output)
        if flag:
            return {
                "solved": True,
                "final_flag": flag,
                "final_summary": latest.summary,
                "stop_reason": "flag_found",
            }

        attempts = state["attempts"]
        if attempts >= state["max_attempts"]:
            return {
                "solved": False,
                "final_summary": latest.summary,
                "stop_reason": "max_attempts_reached",
            }

        next_backend_index = (state.get("backend_index", 0) + 1) % len(state["backend_sequence"])
        return {
            "backend_index": next_backend_index,
            "final_summary": latest.summary,
            "stop_reason": "",
        }

    def after_evaluator(state: ChallengeState) -> str:
        if state.get("solved") or state.get("stop_reason") == "max_attempts_reached":
            return END
        return "run_specialist"

    builder = StateGraph(ChallengeState)
    builder.add_node("route", route_node)
    builder.add_node("run_specialist", specialist_node)
    builder.add_node("evaluate", evaluator_node)
    builder.add_edge(START, "route")
    builder.add_edge("route", "run_specialist")
    builder.add_edge("run_specialist", "evaluate")
    builder.add_conditional_edges("evaluate", after_evaluator)
    return builder.compile(checkpointer=InMemorySaver())


def build_initial_state(
    challenge_name: str,
    challenge_text: str,
    workspace: Path,
    backend_sequence: list[str],
    category_hint: str | None = None,
    target_host: str | None = None,
    challenge_metadata: dict[str, Any] | None = None,
    artifact_paths: list[str] | None = None,
    history: list[dict[str, Any]] | None = None,
    working_memory: dict[str, Any] | None = None,
    max_attempts: int = 4,
) -> ChallengeState:
    if not backend_sequence:
        raise ValueError("backend_sequence must not be empty.")
    return ChallengeState(
        challenge_name=challenge_name,
        challenge_text=challenge_text,
        challenge_metadata=dict(challenge_metadata or {}),
        artifact_paths=list(artifact_paths or []),
        category_hint=category_hint,
        target_host=target_host,
        backend_sequence=backend_sequence,
        backend_index=0,
        attempts=0,
        max_attempts=max_attempts,
        history=list(history or []),
        working_memory=dict(working_memory or _empty_working_memory()),
        workspace=str(workspace.resolve()),
    )


def load_resume_context(workspace: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    history = _load_attempt_history(workspace)
    persisted_memory = _load_working_memory(workspace)
    if not history and not persisted_memory:
        return [], _empty_working_memory()

    audited_history = _prune_resume_history(history)
    working_memory = _merge_resume_memory(audited_history, persisted_memory)
    return audited_history, working_memory


def _get_skill(skills: dict[str, Skill], slug: str) -> Skill:
    if slug not in skills:
        raise KeyError(f"Skill '{slug}' is not available in the registry.")
    return skills[slug]


def _emit_event(
    event_handler: Callable[[str, dict[str, Any]], None] | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if event_handler is None:
        return
    event_handler(event_type, payload)


def _empty_working_memory() -> dict[str, Any]:
    return {
        "memory_version": 1,
        "current_focus": "",
        "confirmed_findings": [],
        "low_value_paths": [],
        "key_commands": [],
        "resume_assessment": {
            "carry_forward": [],
            "questionable_paths": [],
            "restart_guidance": "",
        },
        "handoff_files": [".runs/working-memory.json"],
    }


def _build_attempt_record(request: WorkerRequest, result: WorkerResult) -> dict[str, Any]:
    key_commands = _select_key_commands(result)
    return {
        "attempt": request.attempt_index,
        "backend": result.backend,
        "status": result.status,
        "summary": _truncate_text(result.summary, 320),
        "next_step": _truncate_text(result.next_step, 240),
        "flag": result.flag,
        "evidence": [_truncate_text(item, 180) for item in result.evidence[:4]],
        "commands": key_commands,
        "key_commands": key_commands,
        "inline_scripts": _extract_inline_scripts(key_commands),
        "handoff_files": _attempt_handoff_files(request.workspace, result),
        "event_log_path": result.event_log_path,
        "raw_output_excerpt": _truncate_text(result.raw_output, 320),
    }


def _build_working_memory(
    workspace: Path,
    history: list[dict[str, Any]],
    latest_result: WorkerResult,
) -> dict[str, Any]:
    recent_history = history[-4:]
    confirmed_findings = _dedupe_limited(
        [
            item
            for attempt in recent_history
            for item in attempt.get("evidence", [])
            if item
        ],
        limit=8,
    )
    low_value_paths = _dedupe_limited(
        [
            attempt["summary"]
            for attempt in recent_history
            if attempt.get("status") == "blocked" and attempt.get("summary")
        ],
        limit=6,
    )
    key_commands = _dedupe_limited(
        [
            command
            for attempt in reversed(recent_history)
            for command in attempt.get("key_commands", [])
            if command
        ],
        limit=8,
    )
    handoff_files = _dedupe_limited(
        [".runs/working-memory.json"] + _recent_workspace_files(workspace),
        limit=10,
    )
    return {
        "memory_version": 1,
        "last_updated_attempt": history[-1]["attempt"] if history else 0,
        "current_focus": _truncate_text(latest_result.next_step or latest_result.summary, 220),
        "confirmed_findings": confirmed_findings,
        "low_value_paths": low_value_paths,
        "key_commands": key_commands,
        "inline_scripts": _dedupe_limited(
            [
                _truncate_text(snippet["snippet"], 200)
                for attempt in reversed(recent_history)
                for snippet in attempt.get("inline_scripts", [])
                if snippet.get("snippet")
            ],
            limit=4,
        ),
        "handoff_files": handoff_files,
    }


def _persist_working_memory(workspace: Path, working_memory: dict[str, Any]) -> None:
    memory_path = workspace / WORKING_MEMORY_PATH
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text(
        json.dumps(working_memory, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _persist_attempt_history(workspace: Path, history: list[dict[str, Any]]) -> None:
    history_path = workspace / ATTEMPT_HISTORY_PATH
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(_prune_resume_history(history), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _load_attempt_history(workspace: Path) -> list[dict[str, Any]]:
    history_path = workspace / ATTEMPT_HISTORY_PATH
    if not history_path.exists():
        return []
    payload = json.loads(history_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _load_working_memory(workspace: Path) -> dict[str, Any]:
    memory_path = workspace / WORKING_MEMORY_PATH
    if not memory_path.exists():
        return {}
    payload = json.loads(memory_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return payload


def _select_key_commands(result: WorkerResult) -> list[str]:
    candidates = _dedupe_limited(result.commands + [event.get("command", "") for event in result.command_events], limit=10)
    ranked = sorted(
        [command for command in candidates if command],
        key=lambda command: (_command_priority(command), -len(command)),
    )
    return [_truncate_text(command, 320) for command in ranked[:6]]


def _command_priority(command: str) -> tuple[int, int]:
    lowered = command.lower()
    if any(token in lowered for token in ("python -c", "python3 -c", "node -e", "perl -e", "php -r", "ruby -e")):
        return (0, 0)
    if "<<" in command or len(command) > 180:
        return (1, 0)
    return (2, 0)


def _extract_inline_scripts(commands: list[str]) -> list[dict[str, str]]:
    extracted: list[dict[str, str]] = []
    for command in commands:
        snippet = _extract_inline_script(command)
        if snippet:
            extracted.append(
                {
                    "command": _truncate_text(command, 220),
                    "snippet": _truncate_text(snippet, 240),
                }
            )
        if len(extracted) >= 3:
            break
    return extracted


def _extract_inline_script(command: str) -> str | None:
    patterns = [
        r"(?:python|python3)\s+-c\s+(['\"])(?P<script>.+?)\1",
        r"(?:node)\s+-e\s+(['\"])(?P<script>.+?)\1",
        r"(?:perl)\s+-e\s+(['\"])(?P<script>.+?)\1",
        r"(?:ruby)\s+-e\s+(['\"])(?P<script>.+?)\1",
        r"(?:php)\s+-r\s+(['\"])(?P<script>.+?)\1",
    ]
    for pattern in patterns:
        match = re.search(pattern, command)
        if match:
            return match.group("script")

    heredoc_match = re.search(r"<<['\"]?(?P<tag>[A-Z_]+)['\"]?\s+(?P<body>.+?)\s+(?P=tag)", command)
    if heredoc_match:
        return heredoc_match.group("body")
    if len(command) > 180:
        return command
    return None


def _attempt_handoff_files(workspace: Path, result: WorkerResult) -> list[str]:
    paths: list[str] = []
    if result.event_log_path:
        try:
            paths.append(str(Path(result.event_log_path).resolve().relative_to(workspace.resolve())))
        except Exception:
            paths.append(result.event_log_path)
    paths.extend(_recent_workspace_files(workspace, limit=4))
    return _dedupe_limited(paths, limit=6)


def _recent_workspace_files(workspace: Path, limit: int = 8) -> list[str]:
    candidates: list[tuple[float, str]] = []
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace).as_posix()
        if relative in {"challenge.json", ".discord-thread.json"}:
            continue
        if relative.startswith("artifacts/"):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size > 512_000:
            continue
        candidates.append((stat.st_mtime, relative))
    candidates.sort(reverse=True)
    return [relative for _, relative in candidates[:limit]]


def _dedupe_limited(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
        if len(deduped) >= limit:
            break
    return deduped


def _truncate_text(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3].rstrip()}..."


def _prune_resume_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pruned: list[dict[str, Any]] = []
    for attempt in history[-6:]:
        pruned.append(
            {
                "attempt": attempt.get("attempt"),
                "backend": attempt.get("backend"),
                "status": attempt.get("status"),
                "summary": _truncate_text(str(attempt.get("summary", "")), 220),
                "next_step": _truncate_text(str(attempt.get("next_step", "")), 180),
                "evidence": [_truncate_text(str(item), 140) for item in list(attempt.get("evidence", []))[:3]],
                "key_commands": [_truncate_text(str(item), 180) for item in list(attempt.get("key_commands", []))[:4]],
                "inline_scripts": list(attempt.get("inline_scripts", []))[:2],
                "handoff_files": list(attempt.get("handoff_files", []))[:4],
            }
        )
    return pruned


def _merge_resume_memory(history: list[dict[str, Any]], persisted_memory: dict[str, Any]) -> dict[str, Any]:
    merged = _empty_working_memory()
    if isinstance(persisted_memory, dict):
        merged.update(
            {
                "memory_version": persisted_memory.get("memory_version", merged["memory_version"]),
                "current_focus": persisted_memory.get("current_focus", ""),
                "confirmed_findings": list(persisted_memory.get("confirmed_findings", [])),
                "low_value_paths": list(persisted_memory.get("low_value_paths", [])),
                "key_commands": list(persisted_memory.get("key_commands", [])),
                "inline_scripts": list(persisted_memory.get("inline_scripts", [])),
                "handoff_files": list(persisted_memory.get("handoff_files", merged["handoff_files"])),
            }
        )

    repeated_commands = _find_repeated_commands(history)
    blocked_summaries = [
        attempt["summary"]
        for attempt in history
        if attempt.get("status") == "blocked" and attempt.get("summary")
    ]
    carry_forward = _dedupe_limited(
        merged.get("confirmed_findings", []) + [
            command for command in merged.get("key_commands", []) if command
        ],
        limit=8,
    )
    questionable_paths = _dedupe_limited(
        merged.get("low_value_paths", []) + blocked_summaries + repeated_commands,
        limit=8,
    )
    restart_guidance = _build_restart_guidance(history, merged, repeated_commands)
    merged["resume_assessment"] = {
        "carry_forward": carry_forward,
        "questionable_paths": questionable_paths,
        "restart_guidance": restart_guidance,
    }
    merged["confirmed_findings"] = carry_forward[:6]
    merged["low_value_paths"] = questionable_paths[:6]
    merged["handoff_files"] = _dedupe_limited(
        list(merged.get("handoff_files", [])) + [ATTEMPT_HISTORY_PATH, WORKING_MEMORY_PATH],
        limit=10,
    )
    return merged


def _find_repeated_commands(history: list[dict[str, Any]]) -> list[str]:
    counts: dict[str, int] = {}
    for attempt in history:
        for command in attempt.get("key_commands", []):
            counts[command] = counts.get(command, 0) + 1
    return [
        f"Repeated command pattern: {_truncate_text(command, 160)}"
        for command, count in counts.items()
        if count >= 2
    ][:4]


def _build_restart_guidance(
    history: list[dict[str, Any]],
    merged_memory: dict[str, Any],
    repeated_commands: list[str],
) -> str:
    if not history:
        return ""
    latest = history[-1]
    if repeated_commands:
        return _truncate_text(
            "Do not repeat the same command patterns immediately. Start by reading the handoff files, validating the most concrete prior findings, then pivot away from the repeated path unless new evidence justifies it.",
            220,
        )
    if latest.get("status") == "blocked":
        return _truncate_text(
            f"Previous run stalled on: {latest.get('summary', '')}. Reuse any generated files and focus on a different hypothesis than the blocked path.",
            220,
        )
    if merged_memory.get("current_focus"):
        return _truncate_text(
            f"Resume from the prior focus: {merged_memory['current_focus']}. Verify existing artifacts before issuing new exploratory commands.",
            220,
        )
    return _truncate_text(
        "Resume from the latest useful artifacts in the workspace before starting new reconnaissance.",
        220,
    )
