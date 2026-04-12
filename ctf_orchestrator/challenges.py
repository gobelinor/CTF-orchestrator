"""Normalisation d'un challenge depuis un JSON hand-edited par l'utilisateur.

L'agent ctf-import-agent produit déjà un payload propre avec des clés
explicites (title, description, category, target_host, files, ...). Ce
module existe uniquement pour tolérer les JSON custom que les utilisateurs
écrivent à la main ou qu'ils reçoivent d'exports ad-hoc, et qui utilisent
des clés alternatives comme `name`, `scenario`, `prompt`, `ip`+`port`, etc.
"""

from __future__ import annotations


def normalize_challenge_payload(raw: dict[str, object]) -> dict[str, object]:
    challenge_name = _coalesce_str(raw, "challenge_name", "title", "name")
    challenge_text = _coalesce_str(
        raw,
        "challenge_text",
        "description",
        "scenario",
        "challenge_scenario",
        "prompt",
    )
    category_hint = _coalesce_str(raw, "category_hint", "category")
    target_host = _coalesce_target_host(raw)
    artifact_paths = _coalesce_artifacts(raw)
    challenge_metadata = {
        key: value
        for key, value in raw.items()
        if key
        not in {
            "challenge_name",
            "title",
            "name",
            "challenge_text",
            "description",
            "scenario",
            "challenge_scenario",
            "prompt",
            "category_hint",
            "category",
            "artifact_paths",
            "artifacts",
            "files",
            "target_host",
            "target",
            "ip",
            "port",
        }
    }
    return {
        "challenge_name": challenge_name,
        "challenge_text": challenge_text,
        "category_hint": category_hint,
        "target_host": target_host,
        "artifact_paths": artifact_paths,
        "challenge_metadata": challenge_metadata,
    }


def _coalesce_str(raw: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _coalesce_target_host(raw: dict[str, object]) -> str | None:
    direct = _coalesce_str(raw, "target_host", "target")
    if direct:
        return direct

    ip = _coalesce_str(raw, "ip")
    port = raw.get("port")
    if not ip:
        return None
    if isinstance(port, int):
        return f"{ip}:{port}"
    if isinstance(port, str) and port.strip():
        return f"{ip}:{port.strip()}"
    return ip


def _coalesce_artifacts(raw: dict[str, object]) -> list[str]:
    for key in ("artifact_paths", "artifacts", "files"):
        value = raw.get(key)
        if isinstance(value, list):
            return [str(item) for item in value]
    return []
