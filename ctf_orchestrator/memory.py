from __future__ import annotations

import json
from pathlib import Path
import textwrap
from typing import Any

from .agent_runtime import AgentInvocation, compute_cache_key, invoke_agent
from .skills import Skill, load_skills


MEMORY_SKILL_SLUG = "ctf-memory-agent"
MEMORY_GROUP_ID = "ctf_writeups"
MEMORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "persisted": {"type": "boolean"},
        "group_id": {"type": "string"},
        "episode_name": {"type": "string"},
        "summary": {"type": "string"},
        "skipped_reason": {"type": ["string", "null"]},
    },
    "required": ["persisted", "group_id", "episode_name", "summary"],
    "additionalProperties": False,
}


def persist_challenge_memory(
    *,
    workspace: Path,
    skills_root: Path,
    workers: dict[str, Any],
    backend_sequence: list[str],
    challenge_name: str,
    challenge_text: str,
    category_hint: str | None,
    target_host: str | None,
    final_state: dict[str, Any],
    writeup_markdown: str | None = None,
) -> dict[str, Any] | None:
    skill = _load_memory_skill(skills_root)
    if skill is None:
        return None

    prompt = _build_memory_prompt(
        challenge_name=challenge_name,
        challenge_text=challenge_text,
        category_hint=category_hint,
        target_host=target_host,
        final_state=final_state,
        writeup_markdown=writeup_markdown,
        skill=skill,
    )
    cache_key = compute_cache_key(
        "memory",
        challenge_name,
        final_state.get("final_flag") or "",
        final_state.get("final_summary") or "",
    )
    invocation = AgentInvocation(
        role="memory",
        skill_slug=MEMORY_SKILL_SLUG,
        prompt=prompt,
        schema=MEMORY_SCHEMA,
        workspace=workspace,
        backend_sequence=backend_sequence,
        cache_key=cache_key,
    )
    result = invoke_agent(invocation, workers=workers)
    if not result.ok:
        return None
    return result.payload


def _load_memory_skill(skills_root: Path) -> Skill | None:
    return load_skills(skills_root).get(MEMORY_SKILL_SLUG)


def _build_memory_prompt(
    *,
    challenge_name: str,
    challenge_text: str,
    category_hint: str | None,
    target_host: str | None,
    final_state: dict[str, Any],
    writeup_markdown: str | None,
    skill: Skill,
) -> str:
    working_memory = final_state.get("working_memory", {}) or {}
    history = final_state.get("history", []) or []
    compact_wm = {
        key: working_memory.get(key)
        for key in (
            "active_hypothesis",
            "tested_hypotheses",
            "rejected_hypotheses",
            "promising_leads",
            "backend_performance",
            "useful_artifacts",
            "stagnation_signals",
            "last_strategy_change_reason",
        )
        if key in working_memory
    }
    compact_history = [
        {
            "attempt": attempt.get("attempt"),
            "backend": attempt.get("backend"),
            "status": attempt.get("status"),
            "summary": str(attempt.get("summary", ""))[:260],
            "key_commands": list(attempt.get("key_commands", []))[:4],
        }
        for attempt in history[-6:]
        if isinstance(attempt, dict)
    ]
    return textwrap.dedent(
        f"""
        Challenge name: {challenge_name}
        Category: {category_hint or "unknown"}
        Target host: {target_host or "none"}
        Solved: {final_state.get("solved", False)}
        Final flag: {final_state.get("final_flag") or "unknown"}
        Final summary: {str(final_state.get("final_summary", ""))[:500]}

        Challenge text:
        {challenge_text}

        Compact history:
        {json.dumps(compact_history, indent=2, ensure_ascii=False)}

        Working memory digest:
        {json.dumps(compact_wm, indent=2, ensure_ascii=False)}

        Writeup markdown (may be None):
        {writeup_markdown or ""}

        Agent skill: {skill.name}
        Skill description: {skill.description}

        Skill instructions:
        {skill.instructions}

        Objective:
        - Extract reusable knowledge only (technique, indicators, reusable commands, false signals, platform notes).
        - Persist into Graphiti via MCP in group_id "{MEMORY_GROUP_ID}" using the add_memory tool. Do not touch any other group_id.
        - First, you MAY call search_nodes / search_memory_facts with group_ids=["{MEMORY_GROUP_ID}"] to avoid duplicates.
        - If nothing reusable exists, return persisted=false with a clear skipped_reason. Do not force-write.
        - Return JSON strictly matching the required schema and nothing else.
        """
    ).strip()
