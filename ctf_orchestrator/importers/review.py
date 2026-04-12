from __future__ import annotations

from .models import ImportedChallenge


def render_import_review(challenge: ImportedChallenge) -> str:
    lines = [
        "Import review",
        f"Title: {challenge.title}",
        f"Category: {challenge.category}",
        f"Target host: {challenge.target_host or 'none'}",
        f"Points: {challenge.points if challenge.points is not None else 'unknown'}",
        f"Solves: {challenge.solves if challenge.solves is not None else 'unknown'}",
        f"Files: {len(challenge.files)}",
        f"References: {len(challenge.references)}",
    ]
    if challenge.play_url:
        lines.append(f"Play URL: {challenge.play_url}")
    if challenge.operator_hint:
        lines.append(f"Operator hint: {challenge.operator_hint}")
    if challenge.warnings:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in challenge.warnings)
    return "\n".join(lines)
