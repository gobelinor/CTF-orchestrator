from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import textwrap
from typing import Any

from .agent_runtime import AgentInvocation, invoke_agent
from .skills import Skill, load_skills
from .utils import nullable_str


SUPERVISOR_SKILL_SLUG = "ctf-supervisor-agent"

SUPERVISOR_DECISIONS = {
    "retry_same_backend",
    "retry_same_backend_reframed",
    "switch_backend",
    "stop",
    "request_writeup",
    "request_memory_persist",
    "reassess_category",
    "needs_human",
    "skip",
}

SUPERVISOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": sorted(SUPERVISOR_DECISIONS)},
        "reason": {"type": "string"},
        "next_backend": {"type": ["string", "null"]},
        "next_brief": {"type": "string"},
        "promote_priority": {"type": "boolean"},
        "demote_priority": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["decision", "reason", "next_brief"],
}


@dataclass(frozen=True)
class SupervisorDecision:
    decision: str
    reason: str
    next_backend: str | None
    next_brief: str
    promote_priority: bool
    demote_priority: bool
    notes: str


def decide_post_challenge(
    *,
    skills_root: Path,
    backend_sequence: list[str],
    campaign_context: dict[str, Any],
    challenge_context: dict[str, Any],
    final_state: dict[str, Any],
    workspace: Path | None = None,
) -> SupervisorDecision | None:
    skill = load_skills(skills_root).get(SUPERVISOR_SKILL_SLUG)
    if skill is None:
        return None
    from .workers import build_worker_pool

    try:
        workers = build_worker_pool(backend_sequence)
    except Exception:
        return None

    prompt = _build_supervisor_prompt(
        skill=skill,
        backend_sequence=backend_sequence,
        campaign_context=campaign_context,
        challenge_context=challenge_context,
        final_state=final_state,
    )
    if workspace is None:
        workspace_value = final_state.get("workspace")
        workspace = Path(workspace_value) if workspace_value else Path.cwd() / ".runs" / "supervisor"
    invocation = AgentInvocation(
        role="supervisor",
        skill_slug=SUPERVISOR_SKILL_SLUG,
        prompt=prompt,
        schema=SUPERVISOR_SCHEMA,
        workspace=Path(workspace),
        backend_sequence=backend_sequence,
    )
    result = invoke_agent(invocation, workers=workers)
    if not result.ok or not isinstance(result.payload, dict):
        return None
    decision_value = str(result.payload.get("decision") or "").strip()
    if decision_value not in SUPERVISOR_DECISIONS:
        return None
    return SupervisorDecision(
        decision=decision_value,
        reason=str(result.payload.get("reason") or ""),
        next_backend=nullable_str(result.payload.get("next_backend")),
        next_brief=str(result.payload.get("next_brief") or ""),
        promote_priority=bool(result.payload.get("promote_priority", False)),
        demote_priority=bool(result.payload.get("demote_priority", False)),
        notes=str(result.payload.get("notes") or ""),
    )


def _build_supervisor_prompt(
    *,
    skill: Skill,
    backend_sequence: list[str],
    campaign_context: dict[str, Any],
    challenge_context: dict[str, Any],
    final_state: dict[str, Any],
) -> str:
    digest = _final_state_digest(final_state)
    return textwrap.dedent(
        f"""
        Campaign context:
        {json.dumps(campaign_context, indent=2, ensure_ascii=False)}

        Challenge context:
        {json.dumps(challenge_context, indent=2, ensure_ascii=False)}

        Final state digest:
        {json.dumps(digest, indent=2, ensure_ascii=False)}

        Available backends: {json.dumps(backend_sequence)}
        Graphiti via MCP: available (group_id "ctf_writeups"). You may read it with search_nodes / search_memory_facts. You MUST NOT write anything.

        Supervisor skill: {skill.name}
        Skill description: {skill.description}

        Skill instructions:
        {skill.instructions}

        Objective:
        - You are called only at a decision point between challenges (not between intra-challenge attempts).
        - Choose exactly one of the supported decisions from the skill.
        - Return strictly a JSON object matching the required schema. No prose.
        """
    ).strip()


def _final_state_digest(final_state: dict[str, Any]) -> dict[str, Any]:
    wm = final_state.get("working_memory") or {}
    return {
        "solved": bool(final_state.get("solved")),
        "final_flag": final_state.get("final_flag"),
        "stop_reason": final_state.get("stop_reason"),
        "final_summary": str(final_state.get("final_summary", ""))[:400],
        "pending_decision": final_state.get("pending_decision"),
        "pending_decision_reason": final_state.get("pending_decision_reason"),
        "attempts": final_state.get("attempts"),
        "max_attempts": final_state.get("max_attempts"),
        "active_backend": final_state.get("active_backend"),
        "working_memory_digest": {
            "active_hypothesis": wm.get("active_hypothesis"),
            "tested_hypotheses": wm.get("tested_hypotheses", [])[-6:],
            "rejected_hypotheses": wm.get("rejected_hypotheses", [])[-6:],
            "promising_leads": wm.get("promising_leads", [])[-4:],
            "stagnation_signals": wm.get("stagnation_signals", []),
            "backend_performance": wm.get("backend_performance", {}),
            "last_strategy_change_reason": wm.get("last_strategy_change_reason", ""),
        },
    }


