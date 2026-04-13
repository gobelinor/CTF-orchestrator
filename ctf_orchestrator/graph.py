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

from dataclasses import replace as dataclass_replace

from .agent_runtime import summarize_worker_output, summarizer_enabled, summarizer_threshold
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
    stop_summary: str
    pending_decision: str
    pending_decision_reason: str
    next_brief: str
    writeup_requested: bool
    memory_persist_requested: bool
    workspace: str


DECISION_RETRY_SAME = "retry_same_backend"
DECISION_RETRY_REFRAMED = "retry_same_backend_reframed"
DECISION_SWITCH_BACKEND = "switch_backend"
DECISION_STOP = "stop"
DECISION_REQUEST_WRITEUP = "request_writeup"
DECISION_REQUEST_MEMORY = "request_memory_persist"
DECISION_REASSESS_CATEGORY = "reassess_category"
DECISION_NEEDS_HUMAN = "needs_human"

VALID_DECISIONS = {
    DECISION_RETRY_SAME,
    DECISION_RETRY_REFRAMED,
    DECISION_SWITCH_BACKEND,
    DECISION_STOP,
    DECISION_REQUEST_WRITEUP,
    DECISION_REQUEST_MEMORY,
    DECISION_REASSESS_CATEGORY,
    DECISION_NEEDS_HUMAN,
}

STOP_REASON_SOLVED = "flag_found"
STOP_REASON_MAX_ATTEMPTS = "max_attempts_reached"
STOP_REASON_STAGNATION = "stagnation"
STOP_REASON_NEEDS_HUMAN = "needs_human"
STOP_REASON_AGENT_REQUEST = "agent_requested_stop"


RUNS_DIR = ".runs"
ATTEMPT_HISTORY_PATH = f"{RUNS_DIR}/attempt-history.json"
WORKING_MEMORY_PATH = f"{RUNS_DIR}/working-memory.json"


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
        if (
            summarizer_enabled()
            and result.raw_output
            and len(result.raw_output) > summarizer_threshold()
        ):
            compressed = summarize_worker_output(
                workspace=Path(state["workspace"]),
                workers=workers,
                backend_sequence=state.get("backend_sequence", []),
                role_context=f"{skill.slug} attempt {request.attempt_index}",
                raw_output=result.raw_output,
            )
            if compressed != result.raw_output:
                result = dataclass_replace(result, raw_output=compressed)
                _emit_event(
                    event_handler,
                    "raw_output_summarized",
                    {
                        "attempt": request.attempt_index,
                        "backend": backend_name,
                        "compressed_length": len(compressed),
                    },
                )
        history = list(state.get("history", []))
        attempt_record = _build_attempt_record(request, result)
        history.append(attempt_record)
        previous_memory = dict(state.get("working_memory", {}))
        working_memory = _build_working_memory(
            Path(state["workspace"]),
            history,
            result,
            previous_memory=previous_memory,
        )
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

    def analyze_attempt_node(state: ChallengeState) -> dict[str, Any]:
        latest = WorkerResult.from_payload(state["latest_worker_output"])
        flag = latest.flag or extract_flag(latest.summary) or extract_flag(latest.raw_output)
        working_memory = dict(state.get("working_memory", {}))
        history = list(state.get("history", []))

        analysis = {
            "solved": bool(flag),
            "final_flag": flag,
            "worker_recommended_action": latest.recommended_action or "",
            "worker_failure_reason": latest.failure_reason or "none",
            "worker_needs_human": bool(latest.needs_human),
            "worker_confidence": float(latest.confidence or 0.0),
            "stagnation_signals": list(working_memory.get("stagnation_signals", [])),
            "attempts": state.get("attempts", 0),
            "max_attempts": state.get("max_attempts", 4),
            "history_len": len(history),
        }
        _emit_event(event_handler, "attempt_analyzed", analysis)
        patch: dict[str, Any] = {}
        if flag:
            patch.update(
                {
                    "solved": True,
                    "final_flag": flag,
                    "final_summary": latest.summary,
                }
            )
        return patch

    def decide_next_node(state: ChallengeState) -> dict[str, Any]:
        latest = WorkerResult.from_payload(state["latest_worker_output"])
        working_memory = dict(state.get("working_memory", {}))
        history = list(state.get("history", []))
        attempts = state.get("attempts", 0)
        max_attempts = state.get("max_attempts", 4)
        stagnation = list(working_memory.get("stagnation_signals", []))

        decision, reason = _choose_decision(
            state=state,
            latest=latest,
            working_memory=working_memory,
            history=history,
            attempts=attempts,
            max_attempts=max_attempts,
            stagnation=stagnation,
        )

        brief = _build_attempt_brief(latest, working_memory, decision)
        working_memory = {
            **working_memory,
            "recommended_next_brief": brief,
            "last_strategy_change_reason": reason if decision in {
                DECISION_SWITCH_BACKEND,
                DECISION_RETRY_REFRAMED,
                DECISION_REASSESS_CATEGORY,
            } else working_memory.get("last_strategy_change_reason", ""),
        }

        _persist_working_memory(Path(state["workspace"]), working_memory)

        patch: dict[str, Any] = {
            "pending_decision": decision,
            "pending_decision_reason": reason,
            "next_brief": brief,
            "final_summary": latest.summary,
            "working_memory": working_memory,
        }

        if decision == DECISION_SWITCH_BACKEND:
            patch["backend_index"] = _select_next_backend_index(
                state.get("backend_sequence", []),
                state.get("backend_index", 0),
                working_memory,
                latest,
            )
        elif decision in {DECISION_RETRY_SAME, DECISION_RETRY_REFRAMED}:
            patch["backend_index"] = state.get("backend_index", 0)
        elif decision == DECISION_REQUEST_WRITEUP:
            patch.update(
                {
                    "writeup_requested": True,
                    "solved": bool(state.get("solved") or latest.flag),
                    "final_flag": state.get("final_flag") or latest.flag,
                    "stop_reason": STOP_REASON_SOLVED,
                }
            )
        elif decision == DECISION_REQUEST_MEMORY:
            patch["memory_persist_requested"] = True
        elif decision == DECISION_REASSESS_CATEGORY:
            patch["category_hint"] = None
            patch["backend_index"] = state.get("backend_index", 0)
        elif decision == DECISION_STOP:
            patch["stop_reason"] = patch.get("stop_reason") or _resolve_stop_reason(reason, stagnation, attempts, max_attempts)
            patch["stop_summary"] = _build_stop_summary(state, latest, reason, working_memory)
        elif decision == DECISION_NEEDS_HUMAN:
            patch["stop_reason"] = STOP_REASON_NEEDS_HUMAN
            patch["stop_summary"] = _build_stop_summary(state, latest, reason, working_memory)

        _emit_event(
            event_handler,
            "decision_made",
            {
                "decision": decision,
                "reason": reason,
                "attempts": attempts,
                "stagnation_signals": stagnation,
                "active_backend": state.get("active_backend", ""),
                "stop_reason": patch.get("stop_reason", ""),
            },
        )
        return patch

    def after_decide(state: ChallengeState) -> str:
        decision = state.get("pending_decision", "")
        if state.get("solved") and decision not in {DECISION_REQUEST_MEMORY, DECISION_REQUEST_WRITEUP}:
            return END
        if decision in {DECISION_STOP, DECISION_NEEDS_HUMAN}:
            return END
        if decision == DECISION_REQUEST_WRITEUP:
            return END
        if decision == DECISION_REQUEST_MEMORY:
            return END
        if state.get("attempts", 0) >= state.get("max_attempts", 4):
            return END
        return "run_specialist"

    builder = StateGraph(ChallengeState)
    builder.add_node("route", route_node)
    builder.add_node("run_specialist", specialist_node)
    builder.add_node("analyze_attempt", analyze_attempt_node)
    builder.add_node("decide_next", decide_next_node)
    builder.add_edge(START, "route")
    builder.add_edge("route", "run_specialist")
    builder.add_edge("run_specialist", "analyze_attempt")
    builder.add_edge("analyze_attempt", "decide_next")
    builder.add_conditional_edges("decide_next", after_decide)
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


def _choose_decision(
    *,
    state: ChallengeState,
    latest: WorkerResult,
    working_memory: dict[str, Any],
    history: list[dict[str, Any]],
    attempts: int,
    max_attempts: int,
    stagnation: list[str],
) -> tuple[str, str]:
    if latest.flag or extract_flag(latest.summary) or extract_flag(latest.raw_output):
        if not state.get("writeup_requested"):
            return DECISION_REQUEST_WRITEUP, "flag_found"
        if not state.get("memory_persist_requested"):
            return DECISION_REQUEST_MEMORY, "post_writeup_memory"
        return DECISION_STOP, "post_solve_complete"

    recommended = (latest.recommended_action or "").strip()
    if recommended in VALID_DECISIONS and recommended not in {
        DECISION_REQUEST_WRITEUP,
        DECISION_REQUEST_MEMORY,
    }:
        if recommended == DECISION_NEEDS_HUMAN or latest.needs_human:
            return DECISION_NEEDS_HUMAN, "worker_flagged_human"
        if recommended == DECISION_STOP:
            return DECISION_STOP, "worker_requested_stop"
        if recommended == DECISION_REASSESS_CATEGORY:
            return DECISION_REASSESS_CATEGORY, "worker_flagged_wrong_category"
        if recommended in {DECISION_RETRY_SAME, DECISION_RETRY_REFRAMED, DECISION_SWITCH_BACKEND}:
            if attempts + 1 > max_attempts:
                return DECISION_STOP, "budget_exhausted"
            if stagnation and recommended == DECISION_RETRY_SAME:
                return DECISION_SWITCH_BACKEND, f"stagnation_override:{','.join(stagnation[:2])}"
            return recommended, f"worker_recommended:{recommended}"

    if latest.needs_human:
        return DECISION_NEEDS_HUMAN, "worker_flagged_human"

    failure = latest.failure_reason or "none"
    if failure == "wrong_category":
        return DECISION_REASSESS_CATEGORY, "worker_failure_wrong_category"
    if failure == "needs_human":
        return DECISION_NEEDS_HUMAN, "worker_failure_needs_human"

    reframed_triggers = {"hypothesis_loop", "no_confirmed_hypothesis", "slow_hypothesis_drift", "confidence_downtrend"}
    if any(signal in stagnation for signal in reframed_triggers):
        if attempts + 1 <= max_attempts:
            return DECISION_RETRY_REFRAMED, f"stagnation:{','.join(stagnation[:2])}"
        return DECISION_STOP, "stagnation_budget_exhausted"

    switch_triggers = {"three_consecutive_blocked", "identical_summaries_recent", "command_repetition"}
    if any(signal in stagnation for signal in switch_triggers):
        if attempts + 1 <= max_attempts:
            return DECISION_SWITCH_BACKEND, "stagnation_switch_backend"
        return DECISION_STOP, "stagnation_budget_exhausted"

    if attempts >= max_attempts:
        return DECISION_STOP, "max_attempts_reached"

    if latest.status == "blocked":
        return DECISION_SWITCH_BACKEND, "worker_blocked"
    if latest.status == "needs_retry":
        return DECISION_RETRY_REFRAMED, "worker_needs_retry"

    # Fuse against identical-state loops: if the worker produced the same
    # hypothesis + branch as the previous attempt and did not claim progress,
    # we reframe instead of blindly retrying.
    previous_hypothesis = ""
    for entry in reversed(history[:-1] if history else []):
        if isinstance(entry, dict) and entry.get("hypothesis"):
            previous_hypothesis = str(entry.get("hypothesis", ""))
            break
    if latest.hypothesis and latest.hypothesis == previous_hypothesis and float(latest.confidence or 0.0) < 0.5:
        return DECISION_RETRY_REFRAMED, "identical_repeat_guard"

    if latest.status == "solved":
        if not state.get("writeup_requested"):
            return DECISION_REQUEST_WRITEUP, "solved_no_flag_extracted"
        return DECISION_STOP, "solved_unclear"

    return DECISION_RETRY_REFRAMED, "default_reframe_guard"


def _select_next_backend_index(
    backend_sequence: list[str],
    current_index: int,
    working_memory: dict[str, Any],
    latest: WorkerResult,
) -> int:
    if not backend_sequence:
        return 0
    performance = working_memory.get("backend_performance", {}) or {}

    def score(backend: str) -> tuple[float, float, float]:
        stats = performance.get(backend, {})
        attempts = int(stats.get("attempts", 0))
        solved = int(stats.get("solved", 0))
        blocked = int(stats.get("blocked", 0))
        avg_conf = float(stats.get("avg_confidence", 0.0))
        solve_rate = (solved / attempts) if attempts else 0.0
        block_rate = (blocked / attempts) if attempts else 0.0
        return (-solve_rate, block_rate, -avg_conf)

    current_backend = backend_sequence[current_index % len(backend_sequence)]
    candidates = [b for b in backend_sequence if b != current_backend] or list(backend_sequence)
    best = min(candidates, key=score)
    return backend_sequence.index(best)


def _build_attempt_brief(
    latest: WorkerResult,
    working_memory: dict[str, Any],
    decision: str,
) -> str:
    parts: list[str] = []
    if decision == DECISION_RETRY_REFRAMED:
        parts.append("Reframe the attack. Do not repeat previously tested hypotheses.")
    elif decision == DECISION_SWITCH_BACKEND:
        parts.append("Previous backend stalled or blocked. Fresh start with different backend.")
    elif decision == DECISION_REASSESS_CATEGORY:
        parts.append("Category hint may be wrong. Re-derive category from fresh reading of artifacts.")
    elif decision == DECISION_RETRY_SAME:
        parts.append("Continue the current line of investigation with same backend.")

    rejected = working_memory.get("rejected_hypotheses", []) or []
    if rejected:
        parts.append("Avoid rejected hypotheses: " + "; ".join(rejected[:5]))
    promising = working_memory.get("promising_leads", []) or []
    if promising:
        parts.append("Promising leads: " + "; ".join(promising[:3]))
    active = working_memory.get("active_hypothesis") or latest.hypothesis
    if active and decision != DECISION_RETRY_REFRAMED:
        parts.append(f"Active hypothesis: {active}")
    if latest.next_step:
        parts.append(f"Latest worker next step: {latest.next_step}")
    stagnation = working_memory.get("stagnation_signals", []) or []
    if stagnation:
        parts.append("Stagnation signals: " + ", ".join(stagnation))
    return _truncate_text(" | ".join(parts), 600)


def _resolve_stop_reason(reason: str, stagnation: list[str], attempts: int, max_attempts: int) -> str:
    if attempts >= max_attempts:
        return STOP_REASON_MAX_ATTEMPTS
    if stagnation and "stagnation" in reason:
        return STOP_REASON_STAGNATION
    if reason.startswith("worker_requested") or reason.startswith("post_solve"):
        return STOP_REASON_AGENT_REQUEST
    return reason or STOP_REASON_MAX_ATTEMPTS


def _build_stop_summary(
    state: ChallengeState,
    latest: WorkerResult,
    reason: str,
    working_memory: dict[str, Any],
) -> str:
    lines = [
        f"Stop reason: {reason}",
        f"Attempts used: {state.get('attempts', 0)}/{state.get('max_attempts', 0)}",
        f"Last backend: {state.get('active_backend') or latest.backend}",
        f"Last status: {latest.status}",
    ]
    if latest.hypothesis:
        lines.append(f"Last hypothesis: {latest.hypothesis} ({latest.hypothesis_result})")
    stagnation = working_memory.get("stagnation_signals") or []
    if stagnation:
        lines.append("Stagnation: " + ", ".join(stagnation))
    rejected = working_memory.get("rejected_hypotheses") or []
    if rejected:
        lines.append("Rejected hypotheses: " + "; ".join(rejected[:5]))
    promising = working_memory.get("promising_leads") or []
    if promising:
        lines.append("Promising leads: " + "; ".join(promising[:3]))
    if latest.next_step:
        lines.append(f"Worker suggested next step: {latest.next_step}")
    return _truncate_text(" | ".join(lines), 800)


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


WORKING_MEMORY_VERSION = 2


MEMORY_TESTED_HYPOTHESES_LIMIT = 12
MEMORY_PROMISING_LEADS_LIMIT = 6
MEMORY_REJECTED_HYPOTHESES_LIMIT = 10
MEMORY_USEFUL_ARTIFACTS_LIMIT = 20
MEMORY_REACHABLE_TARGETS_LIMIT = 8
MEMORY_EXPLORATION_BRANCHES_LIMIT = 16
MEMORY_BACKEND_PERFORMANCE_LIMIT = 8
MEMORY_CONFIRMED_FINDINGS_LIMIT = 8
MEMORY_LOW_VALUE_PATHS_LIMIT = 6
MEMORY_KEY_COMMANDS_LIMIT = 8
MEMORY_HANDOFF_FILES_LIMIT = 10
RESUME_HISTORY_DEPTH = 6
RESUME_KEY_COMMANDS_LIMIT = 4
RESUME_EVIDENCE_LIMIT = 3
RESUME_INLINE_SCRIPTS_LIMIT = 2
RESUME_HANDOFF_FILES_LIMIT = 4


def _empty_working_memory() -> dict[str, Any]:
    return {
        "memory_version": WORKING_MEMORY_VERSION,
        "last_updated_attempt": 0,
        "current_focus": "",
        "confirmed_findings": [],
        "low_value_paths": [],
        "key_commands": [],
        "inline_scripts": [],
        "handoff_files": [WORKING_MEMORY_PATH],
        "resume_assessment": {
            "carry_forward": [],
            "questionable_paths": [],
            "restart_guidance": "",
        },
        "active_hypothesis": "",
        "tested_hypotheses": [],
        "rejected_hypotheses": [],
        "promising_leads": [],
        "stagnation_signals": [],
        "backend_performance": {},
        "reachable_targets": [],
        "useful_artifacts": [],
        "last_strategy_change_reason": "",
        "recommended_next_brief": "",
        "exploration_branches": [],
    }


def _build_attempt_record(request: WorkerRequest, result: WorkerResult) -> dict[str, Any]:
    key_commands = _select_key_commands(result)
    return {
        "attempt": request.attempt_index,
        "backend": result.backend,
        "status": result.status,
        "hypothesis": _truncate_text(result.hypothesis or "", 220),
        "hypothesis_result": result.hypothesis_result,
        "confidence": round(float(result.confidence or 0.0), 3),
        "failure_reason": result.failure_reason,
        "branch_id": result.branch_id,
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
    previous_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    recent_history = history[-4:]
    confirmed_findings = _dedupe_limited(
        [
            item
            for attempt in recent_history
            for item in attempt.get("evidence", [])
            if item
        ],
        limit=MEMORY_CONFIRMED_FINDINGS_LIMIT,
    )
    low_value_paths = _dedupe_limited(
        [
            attempt["summary"]
            for attempt in recent_history
            if attempt.get("status") == "blocked" and attempt.get("summary")
        ],
        limit=MEMORY_LOW_VALUE_PATHS_LIMIT,
    )
    key_commands = _dedupe_limited(
        [
            command
            for attempt in reversed(recent_history)
            for command in attempt.get("key_commands", [])
            if command
        ],
        limit=MEMORY_KEY_COMMANDS_LIMIT,
    )
    handoff_files = _dedupe_limited(
        [WORKING_MEMORY_PATH] + _recent_workspace_files(workspace),
        limit=MEMORY_HANDOFF_FILES_LIMIT,
    )

    prior = previous_memory or {}
    tested_hypotheses = list(prior.get("tested_hypotheses", []))
    rejected_hypotheses = list(prior.get("rejected_hypotheses", []))
    promising_leads = list(prior.get("promising_leads", []))
    backend_performance = dict(prior.get("backend_performance", {}))
    reachable_targets = list(prior.get("reachable_targets", []))
    useful_artifacts = list(prior.get("useful_artifacts", []))
    exploration_branches = list(prior.get("exploration_branches", []))

    hypothesis = (latest_result.hypothesis or "").strip()
    if hypothesis:
        record = {
            "hypothesis": _truncate_text(hypothesis, 220),
            "result": latest_result.hypothesis_result or "untested",
            "confidence": round(float(latest_result.confidence or 0.0), 2),
            "attempt": history[-1]["attempt"] if history else 0,
            "backend": latest_result.backend,
            "branch_id": latest_result.branch_id or "",
        }
        if not any(entry.get("hypothesis") == record["hypothesis"] for entry in tested_hypotheses):
            tested_hypotheses.append(record)
        if latest_result.hypothesis_result == "rejected":
            rejected_hypotheses = _dedupe_limited(
                rejected_hypotheses + [record["hypothesis"]],
                limit=MEMORY_REJECTED_HYPOTHESES_LIMIT,
            )
        elif latest_result.hypothesis_result in ("confirmed", "inconclusive") and float(latest_result.confidence or 0) >= 0.5:
            if record["hypothesis"] not in promising_leads:
                promising_leads.append(record["hypothesis"])

    tested_hypotheses = tested_hypotheses[-MEMORY_TESTED_HYPOTHESES_LIMIT:]
    promising_leads = promising_leads[-MEMORY_PROMISING_LEADS_LIMIT:]

    backend_stats = backend_performance.get(latest_result.backend, {"attempts": 0, "solved": 0, "blocked": 0, "avg_confidence": 0.0})
    prev_attempts = int(backend_stats.get("attempts", 0))
    prev_solved = int(backend_stats.get("solved", 0))
    prev_blocked = int(backend_stats.get("blocked", 0))
    prev_avg_conf = float(backend_stats.get("avg_confidence", 0.0))
    new_attempts = prev_attempts + 1
    new_solved = prev_solved + (1 if latest_result.status == "solved" else 0)
    new_blocked = prev_blocked + (1 if latest_result.status == "blocked" else 0)
    new_avg_conf = round(
        ((prev_avg_conf * prev_attempts) + float(latest_result.confidence or 0.0)) / max(new_attempts, 1),
        3,
    )
    backend_performance[latest_result.backend] = {
        "attempts": new_attempts,
        "solved": new_solved,
        "blocked": new_blocked,
        "avg_confidence": new_avg_conf,
        "last_failure_reason": latest_result.failure_reason or "none",
    }

    if latest_result.target_reachable:
        mark = f"{latest_result.backend}:reachable"
        if mark not in reachable_targets:
            reachable_targets.append(mark)
    reachable_targets = reachable_targets[-MEMORY_REACHABLE_TARGETS_LIMIT:]

    for artifact in latest_result.artifacts_produced or []:
        if artifact and artifact not in useful_artifacts:
            useful_artifacts.append(artifact)
    useful_artifacts = useful_artifacts[-MEMORY_USEFUL_ARTIFACTS_LIMIT:]

    branch_id = latest_result.branch_id or ""
    if branch_id:
        existing = next((b for b in exploration_branches if b.get("branch_id") == branch_id), None)
        if existing is None:
            exploration_branches.append(
                {
                    "branch_id": branch_id,
                    "attempts": 1,
                    "last_status": latest_result.status,
                    "last_hypothesis": _truncate_text(hypothesis, 180),
                }
            )
        else:
            existing["attempts"] = int(existing.get("attempts", 0)) + 1
            existing["last_status"] = latest_result.status
            existing["last_hypothesis"] = _truncate_text(hypothesis, 180) or existing.get("last_hypothesis", "")
    exploration_branches = exploration_branches[-MEMORY_EXPLORATION_BRANCHES_LIMIT:]

    if len(backend_performance) > MEMORY_BACKEND_PERFORMANCE_LIMIT:
        # Keep the 8 backends with the best solve rate / lowest block rate to
        # prevent the dict from growing indefinitely on long campaigns.
        def _score(stats: dict[str, Any]) -> tuple[float, float]:
            attempts = int(stats.get("attempts", 0) or 0)
            solved = int(stats.get("solved", 0) or 0)
            blocked = int(stats.get("blocked", 0) or 0)
            solve_rate = (solved / attempts) if attempts else 0.0
            block_rate = (blocked / attempts) if attempts else 0.0
            return (-solve_rate, block_rate)

        backend_performance = dict(
            sorted(backend_performance.items(), key=lambda item: _score(item[1]))[:MEMORY_BACKEND_PERFORMANCE_LIMIT]
        )

    stagnation_signals = _detect_stagnation_signals(history, tested_hypotheses, backend_performance)

    return {
        "memory_version": WORKING_MEMORY_VERSION,
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
        "active_hypothesis": _truncate_text(hypothesis, 220),
        "tested_hypotheses": tested_hypotheses,
        "rejected_hypotheses": rejected_hypotheses,
        "promising_leads": promising_leads,
        "stagnation_signals": stagnation_signals,
        "backend_performance": backend_performance,
        "reachable_targets": reachable_targets,
        "useful_artifacts": useful_artifacts,
        "last_strategy_change_reason": prior.get("last_strategy_change_reason", ""),
        "recommended_next_brief": prior.get("recommended_next_brief", ""),
        "exploration_branches": exploration_branches,
        "resume_assessment": prior.get(
            "resume_assessment",
            {"carry_forward": [], "questionable_paths": [], "restart_guidance": ""},
        ),
    }


def _detect_stagnation_signals(
    history: list[dict[str, Any]],
    tested_hypotheses: list[dict[str, Any]],
    backend_performance: dict[str, Any],
) -> list[str]:
    signals: list[str] = []
    if len(history) >= 3:
        recent = history[-3:]
        if all(attempt.get("status") == "blocked" for attempt in recent):
            signals.append("three_consecutive_blocked")
        summaries = {_truncate_text(str(attempt.get("summary", "")), 120) for attempt in recent}
        if len(summaries) == 1:
            signals.append("identical_summaries_recent")
    if len(tested_hypotheses) >= 4:
        last_four = [entry.get("hypothesis", "") for entry in tested_hypotheses[-4:]]
        if len(set(last_four)) <= 2:
            signals.append("hypothesis_loop")
        last_four_results = [entry.get("result", "") for entry in tested_hypotheses[-4:]]
        if all(result in ("rejected", "inconclusive") for result in last_four_results):
            signals.append("no_confirmed_hypothesis")
        # Slow drift: near-duplicate hypotheses (≥80% similarity) treated as a
        # single line of investigation rather than genuine exploration.
        if _has_slow_hypothesis_drift(last_four):
            signals.append("slow_hypothesis_drift")
    if len(tested_hypotheses) >= 3:
        recent_confidences = [
            float(entry.get("confidence", 0.0) or 0.0) for entry in tested_hypotheses[-3:]
        ]
        if recent_confidences and recent_confidences[0] > 0 and recent_confidences[-1] <= recent_confidences[0] * 0.8:
            if recent_confidences == sorted(recent_confidences, reverse=True):
                signals.append("confidence_downtrend")
    if len(history) >= 3:
        command_counts: dict[str, int] = {}
        for attempt in history[-4:]:
            for command in attempt.get("key_commands", []) or []:
                key = _truncate_text(str(command), 200)
                if not key:
                    continue
                command_counts[key] = command_counts.get(key, 0) + 1
        if any(count >= 3 for count in command_counts.values()):
            signals.append("command_repetition")
    for backend, stats in backend_performance.items():
        attempts = int(stats.get("attempts", 0))
        blocked = int(stats.get("blocked", 0))
        if attempts >= 3 and blocked == attempts:
            signals.append(f"backend_all_blocked:{backend}")
    return signals


def _has_slow_hypothesis_drift(hypotheses: list[str]) -> bool:
    cleaned = [h for h in hypotheses if h]
    if len(cleaned) < 3:
        return False
    for index in range(1, len(cleaned)):
        if _string_similarity(cleaned[index - 1], cleaned[index]) < 0.6:
            return False
    return True


def _string_similarity(a: str, b: str) -> float:
    """Cheap token-level Jaccard similarity, stdlib only."""
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


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
    if int(payload.get("memory_version", 1)) < WORKING_MEMORY_VERSION:
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
    for attempt in history[-RESUME_HISTORY_DEPTH:]:
        pruned.append(
            {
                "attempt": attempt.get("attempt"),
                "backend": attempt.get("backend"),
                "status": attempt.get("status"),
                "hypothesis": attempt.get("hypothesis"),
                "hypothesis_result": attempt.get("hypothesis_result"),
                "confidence": attempt.get("confidence"),
                "failure_reason": attempt.get("failure_reason"),
                "branch_id": attempt.get("branch_id"),
                "summary": _truncate_text(str(attempt.get("summary", "")), 220),
                "next_step": _truncate_text(str(attempt.get("next_step", "")), 180),
                "evidence": [
                    _truncate_text(str(item), 140)
                    for item in list(attempt.get("evidence", []))[:RESUME_EVIDENCE_LIMIT]
                ],
                "key_commands": [
                    _truncate_text(str(item), 180)
                    for item in list(attempt.get("key_commands", []))[:RESUME_KEY_COMMANDS_LIMIT]
                ],
                "inline_scripts": list(attempt.get("inline_scripts", []))[:RESUME_INLINE_SCRIPTS_LIMIT],
                "handoff_files": list(attempt.get("handoff_files", []))[:RESUME_HANDOFF_FILES_LIMIT],
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
                "active_hypothesis": persisted_memory.get("active_hypothesis", ""),
                "tested_hypotheses": list(persisted_memory.get("tested_hypotheses", [])),
                "rejected_hypotheses": list(persisted_memory.get("rejected_hypotheses", [])),
                "promising_leads": list(persisted_memory.get("promising_leads", [])),
                "stagnation_signals": list(persisted_memory.get("stagnation_signals", [])),
                "backend_performance": dict(persisted_memory.get("backend_performance", {})),
                "reachable_targets": list(persisted_memory.get("reachable_targets", [])),
                "useful_artifacts": list(persisted_memory.get("useful_artifacts", [])),
                "last_strategy_change_reason": persisted_memory.get("last_strategy_change_reason", ""),
                "recommended_next_brief": persisted_memory.get("recommended_next_brief", ""),
                "exploration_branches": list(persisted_memory.get("exploration_branches", [])),
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
