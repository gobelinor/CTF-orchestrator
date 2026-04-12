from __future__ import annotations

import json
from pathlib import Path
import textwrap
from typing import Any

from .agent_runtime import AgentInvocation, compute_cache_key, invoke_agent
from .skills import Skill, load_skills


WRITEUP_SKILL_SLUG = "ctf-writeup-agent"
WRITEUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "markdown": {"type": "string"},
    },
    "required": ["markdown"],
    "additionalProperties": False,
}


def generate_writeup_markdown(
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
) -> str | None:
    if not final_state.get("solved"):
        return None

    skill = _load_writeup_skill(skills_root)
    if skill is None:
        return None

    prompt = _build_writeup_prompt(
        challenge_name=challenge_name,
        challenge_text=challenge_text,
        category_hint=category_hint,
        target_host=target_host,
        final_state=final_state,
        skill=skill,
    )
    cache_key = compute_cache_key(
        "writeup",
        challenge_name,
        final_state.get("final_flag") or "",
        final_state.get("final_summary") or "",
    )
    invocation = AgentInvocation(
        role="writeup",
        skill_slug=WRITEUP_SKILL_SLUG,
        prompt=prompt,
        schema=WRITEUP_SCHEMA,
        workspace=workspace,
        backend_sequence=backend_sequence,
        cache_key=cache_key,
    )
    result = invoke_agent(invocation, workers=workers)
    if not result.ok or not isinstance(result.payload, dict):
        return None
    markdown = str(result.payload.get("markdown") or "").strip()
    if not markdown:
        return None
    return markdown if markdown.endswith("\n") else f"{markdown}\n"


def _load_writeup_skill(skills_root: Path) -> Skill | None:
    return load_skills(skills_root).get(WRITEUP_SKILL_SLUG)


def _build_writeup_prompt(
    *,
    challenge_name: str,
    challenge_text: str,
    category_hint: str | None,
    target_host: str | None,
    final_state: dict[str, Any],
    skill: Skill,
) -> str:
    history = final_state.get("history", [])
    latest_output = final_state.get("latest_worker_output", {})
    final_flag = str(final_state.get("final_flag") or "").strip()
    final_summary = _compact_text(str(final_state.get("final_summary", "")), limit=500)

    compact_history = (
        json.dumps(_compact_history(history), indent=2, ensure_ascii=False)
        if isinstance(history, list)
        else "[]"
    )
    latest_payload = (
        json.dumps(_compact_latest_output(latest_output), indent=2, ensure_ascii=False)
        if isinstance(latest_output, dict)
        else "{}"
    )
    commands = json.dumps(_collect_commands(history, latest_output), indent=2, ensure_ascii=False)
    scripts = json.dumps(_collect_inline_scripts(history), indent=2, ensure_ascii=False)

    return textwrap.dedent(
        f"""
        Challenge name: {challenge_name}
        Category: {category_hint or "unknown"}
        Target host: {target_host or "none"}
        Final flag: {final_flag or "unknown"}
        Final summary: {final_summary or "none"}

        Challenge text:
        {challenge_text}

        Latest worker output:
        {latest_payload}

        Compact attempt history:
        {compact_history}

        Collected commands:
        {commands}

        Inline scripts:
        {scripts}

        Specialist skill: {skill.name}
        Skill description: {skill.description}

        Skill instructions:
        {skill.instructions}

        Objective:
        - Write a concise, clear, easy-to-follow CTF writeup in Markdown.
        - Explain the actual solve path, not every dead end.
        - Keep the tone dry and slightly amused; one or two mildly contemptuous lines about the broken design are acceptable.
        - The contempt must target the vulnerability, misuse or challenge design mistake, never the reader.
        - Include exact commands in `## Solve`.
        - Include a short `## Scripts` section only if a script materially helped solve the challenge.
        - Do not fabricate commands, scripts or reasoning not grounded in the provided history.
        - Return JSON that matches the schema exactly.

        Graphiti via MCP is available in this environment. You MAY read it (search_nodes / search_memory_facts with group_ids=["ctf_writeups"]) to confirm a named technique or CVE before referencing it. You MUST NOT write anything into Graphiti yourself — long-term knowledge persistence is the memory agent's job.
        """
    ).strip()


def _compact_history(history: Any, limit: int = 4) -> list[dict[str, Any]]:
    if not isinstance(history, list):
        return []

    items: list[dict[str, Any]] = []
    for attempt in history[-limit:]:
        if not isinstance(attempt, dict):
            continue
        item: dict[str, Any] = {}
        for key in ("attempt", "backend", "status"):
            value = attempt.get(key)
            if value not in (None, ""):
                item[key] = value
        summary = _compact_text(str(attempt.get("summary", "")), limit=260)
        if summary:
            item["summary"] = summary
        evidence = [
            _compact_text(str(entry), limit=180)
            for entry in list(attempt.get("evidence", []))[:4]
            if _compact_text(str(entry), limit=180)
        ]
        if evidence:
            item["evidence"] = evidence
        commands = [
            _compact_text(str(entry), limit=220)
            for entry in list(attempt.get("key_commands", []))[:6]
            if _compact_text(str(entry), limit=220)
        ]
        if commands:
            item["key_commands"] = commands
        scripts = _collect_inline_scripts([attempt], limit=2)
        if scripts:
            item["inline_scripts"] = scripts
        if item:
            items.append(item)
    return items


def _compact_latest_output(latest_output: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("backend", "status", "summary", "next_step", "flag"):
        value = latest_output.get(key)
        if value in (None, ""):
            continue
        if key in {"summary", "next_step"}:
            compact[key] = _compact_text(str(value), limit=280)
        else:
            compact[key] = value

    evidence = [
        _compact_text(str(entry), limit=180)
        for entry in list(latest_output.get("evidence", []))[:4]
        if _compact_text(str(entry), limit=180)
    ]
    if evidence:
        compact["evidence"] = evidence
    commands = _collect_commands([], latest_output)
    if commands:
        compact["commands"] = commands
    return compact


def _collect_commands(history: Any, latest_output: Any, limit: int = 10) -> list[str]:
    commands: list[str] = []
    seen: set[str] = set()

    if isinstance(latest_output, dict):
        for command in list(latest_output.get("commands", [])):
            compact = _compact_text(str(command), limit=320)
            if compact and compact not in seen:
                seen.add(compact)
                commands.append(compact)
                if len(commands) >= limit:
                    return commands

    if isinstance(history, list):
        for attempt in reversed(history):
            if not isinstance(attempt, dict):
                continue
            for command in list(attempt.get("key_commands", [])):
                compact = _compact_text(str(command), limit=320)
                if compact and compact not in seen:
                    seen.add(compact)
                    commands.append(compact)
                    if len(commands) >= limit:
                        return commands
    return commands


def _collect_inline_scripts(history: Any, limit: int = 3) -> list[str]:
    snippets: list[str] = []
    seen: set[str] = set()
    if not isinstance(history, list):
        return snippets
    for attempt in reversed(history):
        if not isinstance(attempt, dict):
            continue
        for item in list(attempt.get("inline_scripts", [])):
            if not isinstance(item, dict):
                continue
            snippet = _compact_text(str(item.get("snippet", "")), limit=1200, preserve_newlines=True)
            if not snippet or snippet in seen:
                continue
            seen.add(snippet)
            snippets.append(snippet)
            if len(snippets) >= limit:
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
