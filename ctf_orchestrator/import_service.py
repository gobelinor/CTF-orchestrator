from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha1
from pathlib import Path
from typing import Any

from .import_agent import normalize_via_agent
from .importers import (
    DiscoveredChallenge,
    ImportRequest,
    ImportedChallenge,
    SourceDocument,
    load_source_document,
)


@dataclass(frozen=True)
class BoardImportContext:
    import_request: ImportRequest
    document: SourceDocument
    candidates: list[DiscoveredChallenge]
    board_source_key: str
    source_label: str


@dataclass(frozen=True)
class ImportedChallengeRecord:
    candidate: DiscoveredChallenge
    imported: ImportedChallenge | None
    payload: dict[str, Any] | None
    error: str | None = None

    @property
    def successful(self) -> bool:
        return self.imported is not None and self.payload is not None and self.error is None


def load_board_context(import_request: ImportRequest) -> BoardImportContext:
    document = load_source_document(import_request)
    imported_challenges = _dispatch_import_agent(
        document=document,
        selected_challenge=None,
        list_mode=True,
        start_instance=False,
        session_cookie=import_request.session_cookie,
    )
    if not imported_challenges:
        raise SystemExit(
            "Import agent returned no challenges. Ensure claude/codex is available and the source contains a CTF challenge."
        )
    candidates = [_to_discovered_challenge(imported, document) for imported in imported_challenges]
    source_label = document.fetched_url or document.source_label or _request_source_label(import_request)
    return BoardImportContext(
        import_request=import_request,
        document=document,
        candidates=candidates,
        board_source_key=build_board_source_key(import_request, document),
        source_label=source_label,
    )


def select_candidates(
    candidates: list[DiscoveredChallenge],
    queries: list[str] | None,
) -> list[DiscoveredChallenge]:
    if not queries:
        return list(candidates)

    selected: list[DiscoveredChallenge] = []
    seen_titles: set[str] = set()
    for query in queries:
        match = _select_single_candidate(candidates, query)
        normalized_title = match.title.lower()
        if normalized_title in seen_titles:
            continue
        seen_titles.add(normalized_title)
        selected.append(match)
    return selected


def import_candidate(
    context: BoardImportContext,
    candidate: DiscoveredChallenge,
    *,
    start_instance: bool | None = None,
) -> ImportedChallenge:
    request = context.import_request
    if start_instance is not None:
        request = replace(request, start_instance=start_instance)
    imported = _dispatch_single_import(
        document=context.document,
        candidate_title=candidate.title,
        start_instance=bool(request.start_instance),
        session_cookie=request.session_cookie,
    )
    imported = _enrich_imported_challenge(
        imported=imported,
        candidate=candidate,
        context=context,
        import_request=request,
    )
    return imported


def import_selected_candidates(
    context: BoardImportContext,
    *,
    queries: list[str] | None = None,
    start_instance: bool | None = None,
) -> list[ImportedChallengeRecord]:
    records: list[ImportedChallengeRecord] = []
    for candidate in select_candidates(context.candidates, queries):
        try:
            imported = import_candidate(context, candidate, start_instance=start_instance)
            payload = imported.to_payload()
            error = validate_instance_access(
                replace(context.import_request, start_instance=start_instance)
                if start_instance is not None
                else context.import_request,
                imported,
            )
            if error:
                records.append(
                    ImportedChallengeRecord(
                        candidate=candidate,
                        imported=imported,
                        payload=None,
                        error=error,
                    )
                )
                continue
            records.append(
                ImportedChallengeRecord(
                    candidate=candidate,
                    imported=imported,
                    payload=payload,
                    error=None,
                )
            )
        except Exception as exc:
            records.append(
                ImportedChallengeRecord(
                    candidate=candidate,
                    imported=None,
                    payload=None,
                    error=str(exc),
                )
            )
    return records


def build_board_source_key(import_request: ImportRequest, document: SourceDocument) -> str:
    source_label = document.fetched_url or document.source_label or _request_source_label(import_request)
    digest = sha1(source_label.encode("utf-8")).hexdigest()[:12]
    return f"board-{digest}"


def validate_instance_access(import_request: ImportRequest, imported: ImportedChallenge) -> str | None:
    if not import_request.start_instance:
        return None
    if imported.target_host:
        return None

    metadata = imported.import_metadata if isinstance(imported.import_metadata, dict) else {}
    start_result = str(metadata.get("start_instance_result") or "unknown")
    details = list(imported.warnings)
    detail_suffix = ""
    if details:
        detail_suffix = f" Details: {'; '.join(details)}"
    return (
        f"failed to acquire instance access for '{imported.title}' "
        f"(start_instance_result={start_result}).{detail_suffix}"
    )


def _dispatch_import_agent(
    *,
    document: SourceDocument,
    selected_challenge: str | None,
    list_mode: bool,
    start_instance: bool = False,
    session_cookie: str | None = None,
) -> list[ImportedChallenge] | None:
    return normalize_via_agent(
        document=document,
        skills_root=_default_skills_root(),
        selected_challenge=selected_challenge,
        list_mode=list_mode,
        start_instance=start_instance,
        session_cookie=session_cookie,
    )


def _dispatch_single_import(
    *,
    document: SourceDocument,
    candidate_title: str,
    start_instance: bool = False,
    session_cookie: str | None = None,
) -> ImportedChallenge:
    results = _dispatch_import_agent(
        document=document,
        selected_challenge=candidate_title,
        list_mode=False,
        start_instance=start_instance,
        session_cookie=session_cookie,
    )
    if not results:
        raise SystemExit(
            f"Import agent failed to normalize challenge '{candidate_title}'."
        )
    # Prefer the entry whose title matches selected_challenge, otherwise first.
    for entry in results:
        if entry.title.strip().lower() == candidate_title.strip().lower():
            return entry
    return results[0]


def _default_skills_root() -> Path:
    return Path(__file__).resolve().parents[1] / "skills"


def _to_discovered_challenge(imported: ImportedChallenge, document: SourceDocument) -> DiscoveredChallenge:
    return DiscoveredChallenge(
        title=imported.title,
        text_block=imported.description or imported.title,
        challenge_id=_extract_ctfd_id(imported),
        category=imported.category or None,
        points=imported.points,
        solves=imported.solves,
        source_label=document.fetched_url or document.source_label,
        warnings=list(imported.warnings),
    )


def _extract_ctfd_id(imported: ImportedChallenge) -> int | None:
    metadata = imported.import_metadata if isinstance(imported.import_metadata, dict) else {}
    for key in ("ctfd_challenge_id", "challenge_id"):
        value = metadata.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _enrich_imported_challenge(
    *,
    imported: ImportedChallenge,
    candidate: DiscoveredChallenge,
    context: BoardImportContext,
    import_request: ImportRequest,
) -> ImportedChallenge:
    metadata = dict(imported.import_metadata)
    metadata["board_source_key"] = context.board_source_key
    if candidate.challenge_id is not None:
        metadata.setdefault("challenge_id", candidate.challenge_id)
    metadata.setdefault("imported_via_agent", True)
    metadata.setdefault("import_source", context.document.fetched_url or context.document.source_label)

    if metadata.get("explicit_difficulty") is None:
        difficulty = metadata.get("difficulty_hint")
        if isinstance(difficulty, str) and difficulty.strip().lower() in {"easy", "medium", "hard"}:
            metadata["explicit_difficulty"] = difficulty.strip().lower()

    metadata.setdefault("instance_required", bool(metadata.get("instance_required", False)))
    metadata.setdefault("start_instance_supported", bool(metadata.get("start_instance_supported", False)))
    metadata["instance_source"] = "ctfd_container" if metadata.get("instance_required") else "none"
    metadata["start_instance_requested"] = bool(import_request.start_instance)
    return replace(imported, import_metadata=metadata)


def _request_source_label(import_request: ImportRequest) -> str:
    if import_request.input_file is not None:
        return str(import_request.input_file)
    if import_request.source is not None:
        return str(import_request.source)
    return str(Path.cwd())


def _select_single_candidate(
    candidates: list[DiscoveredChallenge],
    query: str,
) -> DiscoveredChallenge:
    normalized_query = query.strip().lower()
    exact = [candidate for candidate in candidates if candidate.title.lower() == normalized_query]
    if len(exact) == 1:
        return exact[0]

    partial = [candidate for candidate in candidates if normalized_query in candidate.title.lower()]
    if len(partial) == 1:
        return partial[0]

    if not partial and not exact:
        raise SystemExit(f"Unable to select a unique challenge for query: {query}")
    raise SystemExit(f"Multiple challenges match query: {query}")
