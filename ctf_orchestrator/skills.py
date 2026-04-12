from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable

import yaml
from yaml import YAMLError


FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

CATEGORY_ALIASES: dict[str, str] = {
    "cryptography": "crypto",
    "forensic": "forensics",
    "dfir": "forensics",
    "android": "mobile",
    "ios": "mobile",
    "binexp": "pwn",
    "binary exploitation": "pwn",
    "reversing": "reverse",
    "reverse engineering": "reverse",
    "rev": "reverse",
    "steganography": "stego",
    "web exploitation": "web",
    "smart contract": "blockchain",
    "smart contracts": "blockchain",
    "evm": "blockchain",
    "rf": "hardware",
    "hardware rf": "hardware",
    "hardware/rf": "hardware",
    "embedded": "hardware",
    "iot": "hardware",
    "pyjail": "jail",
    "sandbox": "jail",
    "sandbox escape": "jail",
    "ai": "ai_llm",
    "ai llm": "ai_llm",
    "ai security": "ai_llm",
    "ai ml": "ai_llm",
    "ml": "ai_llm",
    "machine learning": "ai_llm",
    "llm": "ai_llm",
    "llm security": "ai_llm",
    "prompt injection": "ai_llm",
    "jailbreak": "ai_llm",
    "adversarial": "ai_llm",
    "adversarial ml": "ai_llm",
    "securite ia": "ai_llm",
    "securite_ia": "ai_llm",
    "ia": "ai_llm",
    "genai": "ai_llm",
    "gen ai": "ai_llm",
}

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "crypto": ("rsa", "cipher", "encrypt", "decrypt", "lattice"),
    "reverse": ("elf", "ghidra", "decompile", "crackme"),
    "web": ("sql", "xss", "ssrf", "jwt", "cookie"),
    "pwn": ("overflow", "rop", "shellcode", "heap"),
    "forensics": ("pcap", "memory dump", "disk image"),
    "osint": ("geolocation", "username", "public profile"),
    "stego": ("steganography", "lsb", "spectrogram"),
    "mobile": ("apk", "jadx", "frida", "adb"),
    "blockchain": ("solidity", "evm", "smart contract"),
    "cloud": ("aws", "iam", "s3", "kubernetes"),
    "hardware": ("firmware", "uart", "jtag", "sdr"),
    "jail": ("pyjail", "seccomp", "sandbox escape"),
    "ai_llm": ("prompt injection", "system prompt", "jailbreak", "safetensors", "llm", "chatbot", "embedding"),
}

_DEFAULT_CATEGORY_TO_SKILL = {
    "ai_llm": "ctf-ai-llm-solver",
    "blockchain": "ctf-blockchain-solver",
    "cloud": "ctf-cloud-solver",
    "crypto": "ctf-crypto-solver",
    "forensics": "ctf-forensics-solver",
    "hardware": "ctf-hardware-rf-solver",
    "jail": "ctf-jail-solver",
    "misc": "ctf-misc-solver",
    "mobile": "ctf-mobile-solver",
    "osint": "ctf-osint-solver",
    "pwn": "ctf-pwn-solver",
    "reverse": "ctf-reverse-solver",
    "stego": "ctf-stego-solver",
    "web": "ctf-web-solver",
}

# Exposed for backwards compat. Prefer build_category_to_skill(skills).
CATEGORY_TO_SKILL = dict(_DEFAULT_CATEGORY_TO_SKILL)

CORE_SKILL_SLUG = "ctf-core-methodology"


@dataclass(frozen=True)
class Skill:
    slug: str
    name: str
    description: str
    instructions: str
    path: Path


def _parse_skill_file(path: Path) -> Skill:
    raw_text = path.read_text(encoding="utf-8")
    match = FRONT_MATTER_RE.match(raw_text)
    metadata: dict[str, str] = {}
    instructions = raw_text.strip()
    if match:
        metadata = _parse_front_matter(match.group(1))
        instructions = match.group(2).strip()
    slug = path.parent.name
    return Skill(
        slug=slug,
        name=metadata.get("name", slug),
        description=metadata.get("description", ""),
        instructions=instructions,
        path=path,
    )


def load_skills(root: Path) -> dict[str, Skill]:
    return {
        skill.slug: skill
        for skill in (_parse_skill_file(path) for path in root.glob("*/SKILL.md"))
    }


def route_category(
    challenge_text: str,
    category_hint: str | None = None,
    *,
    llm_fallback: bool = True,
) -> tuple[str, str]:
    if category_hint:
        normalized = _normalize_category_hint(category_hint)
        if normalized in CATEGORY_TO_SKILL:
            return normalized, f"Used explicit category hint '{category_hint.strip()}' as '{normalized}'."

    text = challenge_text.lower()
    scores = {
        category: sum(keyword in text for keyword in keywords)
        for category, keywords in CATEGORY_KEYWORDS.items()
    }
    best_category = max(scores, key=scores.get, default="misc")
    best_score = scores.get(best_category, 0)
    if best_score > 0:
        return best_category, f"Matched keywords for '{best_category}' with score {best_score}."

    if llm_fallback:
        llm_category, llm_reason = _llm_route_category(challenge_text)
        if llm_category:
            return llm_category, llm_reason

    return "misc", "No keyword match, falling back to misc."


_ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": sorted(set(CATEGORY_ALIASES.values()) | set(_DEFAULT_CATEGORY_TO_SKILL) | {"misc"}),
        },
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["category", "reason"],
}


def _llm_route_category(challenge_text: str) -> tuple[str | None, str]:
    """Cheap LLM classifier fallback when keywords miss.

    Uses the unified agent runtime with the "route" role. Returns (None, "")
    silently on any failure — keyword fallback stays the last resort.
    """
    try:
        from .agent_runtime import AgentInvocation, invoke_agent
        from .workers import build_worker_pool
    except Exception:
        return None, ""

    try:
        workers = build_worker_pool(["claude", "codex"])
    except Exception:
        return None, ""

    snippet = challenge_text.strip()
    if len(snippet) > 4000:
        snippet = snippet[:4000] + "\n... [truncated]"
    prompt = (
        "You are a CTF category classifier. Choose exactly one category from: "
        + ", ".join(sorted(set(CATEGORY_ALIASES.values()) | set(_DEFAULT_CATEGORY_TO_SKILL) | {"misc"}))
        + ".\n\nChallenge text:\n---\n"
        + snippet
        + "\n---\n\nReturn strict JSON: {category, confidence (0..1), reason}."
    )
    invocation = AgentInvocation(
        role="route",
        skill_slug="ctf-category-router",
        prompt=prompt,
        schema=_ROUTE_SCHEMA,
        workspace=Path.cwd() / ".runs" / "route",
        backend_sequence=["claude", "codex"],
    )
    try:
        result = invoke_agent(invocation, workers=workers)
    except Exception:
        return None, ""
    if not result.ok or not isinstance(result.payload, dict):
        return None, ""
    category = str(result.payload.get("category") or "").strip().lower()
    if not category:
        return None, ""
    reason = str(result.payload.get("reason") or "llm classifier")
    return category, f"LLM fallback classifier: {reason}"


def build_category_to_skill(skills: dict[str, Skill]) -> dict[str, str]:
    """Compute the category → skill slug mapping by auto-discovery.

    Strategy:
    1. Respect any explicit `category` field in a skill's front-matter.
    2. Fall back to slug-based inference: `ctf-<category>-solver` or
       `ctf-<category>` patterns.
    3. Merge with the static default mapping so legacy slugs keep working.
    """
    mapping = dict(_DEFAULT_CATEGORY_TO_SKILL)
    for slug, skill in skills.items():
        declared = _extract_declared_category(skill)
        if declared:
            mapping[declared] = slug
            continue
        inferred = _infer_category_from_slug(slug)
        if inferred and inferred not in mapping:
            mapping[inferred] = slug
    return mapping


def _extract_declared_category(skill: Skill) -> str | None:
    # Re-read the front matter to look for a non-standard `category` key.
    try:
        raw_text = skill.path.read_text(encoding="utf-8")
    except Exception:
        return None
    match = FRONT_MATTER_RE.match(raw_text)
    if not match:
        return None
    metadata = _parse_front_matter(match.group(1))
    category = metadata.get("category") or metadata.get("ctf_category")
    if not category:
        return None
    # Normalize dashes/spaces into a space-separated form for alias lookup,
    # but preserve underscores since they're the canonical separator for
    # multi-word custom categories like "ai_adversarial".
    raw = str(category).strip().lower()
    for_alias = re.sub(r"[\s/-]+", " ", raw).replace("_", " ")
    aliased = CATEGORY_ALIASES.get(for_alias)
    if aliased is not None:
        return aliased
    return raw


def _infer_category_from_slug(slug: str) -> str | None:
    if not slug.startswith("ctf-"):
        return None
    remainder = slug[4:]
    for suffix in ("-solver", "-agent", "-skill"):
        if remainder.endswith(suffix):
            remainder = remainder[: -len(suffix)]
            break
    if not remainder:
        return None
    normalized = remainder.replace("-", " ").strip()
    return CATEGORY_ALIASES.get(normalized, normalized)


def resolve_specialist_skill(category: str, skills: dict[str, Skill]) -> Skill:
    mapping = build_category_to_skill(skills)
    skill_slug = mapping.get(category, "ctf-misc-solver")
    if skill_slug in skills:
        return skills[skill_slug]
    if "ctf-misc-solver" in skills:
        return skills["ctf-misc-solver"]
    available = ", ".join(sorted(skills))
    raise KeyError(f"Unable to resolve a skill for category '{category}'. Available: {available}")


def resolve_core_skill(skills: dict[str, Skill]) -> Skill | None:
    return skills.get(CORE_SKILL_SLUG)


def summarize_skill_inventory(skills: Iterable[Skill]) -> str:
    return ", ".join(sorted(skill.slug for skill in skills))


def _normalize_category_hint(category_hint: str) -> str:
    normalized = re.sub(r"[\s/_-]+", " ", category_hint.strip().lower())
    return CATEGORY_ALIASES.get(normalized, normalized)


def _parse_front_matter(text: str) -> dict[str, str]:
    try:
        parsed = yaml.safe_load(text) or {}
        if isinstance(parsed, dict):
            return {str(key): str(value) for key, value in parsed.items()}
    except YAMLError:
        pass

    metadata: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"').strip("'")
    return metadata
