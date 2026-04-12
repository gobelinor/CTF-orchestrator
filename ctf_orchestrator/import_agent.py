from __future__ import annotations

from pathlib import Path
import textwrap
from typing import Any

from .agent_runtime import AgentInvocation, compute_cache_key, invoke_agent
from .importers.models import ImportedChallenge, SourceDocument
from .skills import Skill, load_skills
from .utils import nullable_int, nullable_str


IMPORT_AGENT_SKILL_SLUG = "ctf-import-agent"

IMPORT_AGENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["single", "list"]},
        "challenges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {"type": ["string", "null"]},
                    "category_confidence": {"type": "number"},
                    "target_host": {"type": ["string", "null"]},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "play_url": {"type": ["string", "null"]},
                    "points": {"type": ["number", "null"]},
                    "solves": {"type": ["number", "null"]},
                    "references": {"type": "array", "items": {"type": "string"}},
                    "difficulty_hint": {"type": ["string", "null"]},
                    "instance_required": {"type": "boolean"},
                    "start_instance_supported": {"type": "boolean"},
                    "operator_hint": {"type": ["string", "null"]},
                    "source_snippet": {"type": ["string", "null"]},
                    "warnings": {"type": "array", "items": {"type": "string"}},
                    "challenge_metadata": {"type": "object"},
                },
                "required": ["title", "description"],
            },
        },
    },
    "required": ["mode", "challenges"],
}


def normalize_via_agent(
    *,
    document: SourceDocument,
    skills_root: Path,
    selected_challenge: str | None = None,
    list_mode: bool = False,
    preferred_backend: str | None = None,
    start_instance: bool = False,
    session_cookie: str | None = None,
) -> list[ImportedChallenge] | None:
    skill = load_skills(skills_root).get(IMPORT_AGENT_SKILL_SLUG)
    if skill is None:
        return None

    backend_sequence = _resolve_backends(preferred_backend)
    from .workers import build_worker_pool

    try:
        workers = build_worker_pool(backend_sequence)
    except Exception:
        return None

    prompt = _build_import_prompt(
        document=document,
        skill=skill,
        selected_challenge=selected_challenge,
        list_mode=list_mode,
        start_instance=start_instance,
        session_cookie=session_cookie,
    )
    cache_key = compute_cache_key(
        "import",
        document.fetched_url or document.source_label or "",
        (document.raw_text or "")[:2048],
        selected_challenge or "",
        int(bool(list_mode)),
        int(bool(start_instance)),
    )
    workspace = _resolve_import_workspace()
    invocation = AgentInvocation(
        role="import",
        skill_slug=IMPORT_AGENT_SKILL_SLUG,
        prompt=prompt,
        schema=IMPORT_AGENT_SCHEMA,
        workspace=workspace,
        backend_sequence=backend_sequence,
        cache_key=cache_key,
    )
    result = invoke_agent(invocation, workers=workers)
    if not result.ok or not isinstance(result.payload, dict):
        return None
    challenges_field = result.payload.get("challenges")
    if not isinstance(challenges_field, list):
        return None
    imported: list[ImportedChallenge] = []
    for entry in challenges_field:
        if isinstance(entry, dict):
            imported.append(_entry_to_imported_challenge(entry, document))
    return imported or None


def _resolve_import_workspace() -> Path:
    path = Path.cwd() / ".runs" / "import"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_backends(preferred: str | None) -> list[str]:
    if preferred:
        return [preferred]
    return ["claude", "codex"]


def _build_import_prompt(
    *,
    document: SourceDocument,
    skill: Skill,
    selected_challenge: str | None,
    list_mode: bool,
    start_instance: bool,
    session_cookie: str | None,
) -> str:
    content_kind = _infer_content_kind(document)
    raw_content = document.raw_text
    if len(raw_content) > 20000:
        raw_content = raw_content[:20000] + "\n... [truncated]"
    raw_html_block = "(not available)"
    if start_instance and document.raw_html:
        html = document.raw_html
        if len(html) > 20000:
            html = html[:20000] + "\n... [truncated]"
        raw_html_block = html
    cookie_block = "none"
    if session_cookie:
        cookie_block = session_cookie if "=" in session_cookie else f"session={session_cookie}"
    instance_directive = (
        "TRUE — you MUST drive the instance launch yourself before returning."
        if start_instance
        else "FALSE — do not attempt to start any instance; just normalize."
    )
    return textwrap.dedent(
        f"""
        Source label: {document.source_label}
        Fetched URL: {document.fetched_url or "none"}
        Content kind: {content_kind}
        List mode: {str(bool(list_mode)).lower()}
        Selected challenge: {selected_challenge or "none"}
        Start instance required: {instance_directive}
        Session cookie (use as Cookie header on any HTTP call): {cookie_block}

        Raw content (text extraction):
        ---
        {raw_content}
        ---

        Raw HTML (only when start_instance=true; use for CSRF / form extraction):
        ---
        {raw_html_block}
        ---

        Agent skill: {skill.name}
        Skill description: {skill.description}

        Skill instructions:
        {skill.instructions}

        Objective:
        - Normalize the raw content into the structured schema required by ctf-import-agent.
        - If list_mode is true, return every detected challenge. Otherwise return exactly one entry (pick selected_challenge if provided, else the single most likely match).
        - If "Start instance required" is TRUE, you MUST figure out the platform-specific way to launch the challenge instance (CTFd container, custom "deploy" button, /api call, click-through, anything) using whatever HTTP/shell tooling you have, AUTHENTICATED with the session cookie if provided. You then wait for the target host to become live, extract `host:port` (or full URL), and populate `target_host` in the returned challenge. Set `start_instance_supported=true` and include the raw access info in `challenge_metadata.instance_access`. If the instance cannot be launched, explain why in `warnings` and leave `target_host` null.
        - If "Start instance required" is FALSE, do NOT perform any side-effect HTTP calls. Only read and normalize.
        - Graphiti via MCP is available if you want to check whether a challenge with the same title already exists in group_id "ctf_writeups". Do NOT write anything into Graphiti yourself.
        - Return strictly a JSON object matching the expected schema. No markdown, no prose.
        """
    ).strip()


def _infer_content_kind(document: SourceDocument) -> str:
    if document.raw_html:
        return "html"
    source_label = (document.source_label or "").lower()
    if source_label.endswith(".json"):
        return "json"
    if document.source_type:
        return document.source_type
    return "text"


def _entry_to_imported_challenge(entry: dict[str, Any], document: SourceDocument) -> ImportedChallenge:
    metadata: dict[str, Any] = {}
    raw_meta = entry.get("challenge_metadata")
    if isinstance(raw_meta, dict):
        metadata.update(raw_meta)
    difficulty = entry.get("difficulty_hint")
    if difficulty:
        metadata["explicit_difficulty"] = str(difficulty).lower()
    if entry.get("instance_required") is not None:
        metadata["instance_required"] = bool(entry.get("instance_required"))
    if entry.get("start_instance_supported") is not None:
        metadata["start_instance_supported"] = bool(entry.get("start_instance_supported"))
    metadata.setdefault("import_source", document.fetched_url or document.source_label)
    metadata.setdefault("imported_via_agent", True)

    confidence = entry.get("category_confidence")
    if isinstance(confidence, (int, float)):
        metadata["category_confidence"] = float(confidence)

    warnings = [str(item) for item in (entry.get("warnings") or []) if item]

    return ImportedChallenge(
        title=str(entry.get("title") or "").strip() or "Untitled challenge",
        description=str(entry.get("description") or "").strip(),
        category=str(entry.get("category") or "misc").strip() or "misc",
        target_host=nullable_str(entry.get("target_host")),
        files=[str(f) for f in (entry.get("files") or []) if f],
        operator_hint=nullable_str(entry.get("operator_hint")),
        points=nullable_int(entry.get("points")),
        solves=nullable_int(entry.get("solves")),
        play_url=nullable_str(entry.get("play_url")) or document.fetched_url,
        references=[str(r) for r in (entry.get("references") or []) if r],
        source_snippet=nullable_str(entry.get("source_snippet")),
        import_metadata=metadata,
        warnings=warnings,
    )
