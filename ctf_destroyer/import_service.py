from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha1
from pathlib import Path
import re
from typing import Any

from .importers import (
    DiscoveredChallenge,
    ImportRequest,
    ImportedChallenge,
    SourceDocument,
    discover_text_challenges,
    import_ctfd_challenge,
    load_source_document,
    try_discover_ctfd_challenges,
)
from .importers.ctfd import _extract_csrf_nonce
from .importers.text import import_text_challenge


EXPLICIT_DIFFICULTY_RE = re.compile(r"\b(very easy|easy|medium|hard)\b", re.IGNORECASE)
INSTANCE_HINT_RE = re.compile(r"(?:tcp/)?<host>|instance|container", re.IGNORECASE)


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
    raw_candidates = try_discover_ctfd_challenges(document, import_request) or discover_text_challenges(document)
    candidates = annotate_candidates_for_listing(raw_candidates, document, import_request)
    source_label = document.fetched_url or document.source_label or _request_source_label(import_request)
    return BoardImportContext(
        import_request=import_request,
        document=document,
        candidates=candidates,
        board_source_key=build_board_source_key(import_request, document),
        source_label=source_label,
    )


def annotate_candidates_for_listing(
    candidates: list[DiscoveredChallenge],
    document: SourceDocument,
    import_request: ImportRequest,
) -> list[DiscoveredChallenge]:
    inspect_request = replace(import_request, start_instance=False)
    annotated: list[DiscoveredChallenge] = []
    for candidate in candidates:
        warnings = list(getattr(candidate, "warnings", []))
        try:
            imported = _import_candidate(candidate, document, inspect_request)
            warnings = list(imported.warnings)
        except Exception:
            pass
        annotated.append(
            replace(
                candidate,
                warnings=warnings,
            )
        )
    return annotated


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
    imported = _import_candidate(candidate, context.document, request)
    return _enrich_imported_challenge(
        imported=imported,
        candidate=candidate,
        context=context,
        import_request=request,
    )


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


def _import_candidate(
    candidate: DiscoveredChallenge,
    document: SourceDocument,
    import_request: ImportRequest,
) -> ImportedChallenge:
    return import_ctfd_challenge(candidate, document, import_request) or import_text_challenge(candidate, document)


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

    explicit_difficulty = _detect_explicit_difficulty(imported, candidate)
    if explicit_difficulty:
        metadata["explicit_difficulty"] = explicit_difficulty

    instance_required = _infer_instance_required(imported, candidate, context.document)
    metadata["instance_required"] = instance_required
    metadata["instance_source"] = "ctfd_container" if instance_required else "none"
    metadata["start_instance_supported"] = bool(candidate.challenge_id is not None and _extract_csrf_nonce(context.document))
    metadata["start_instance_requested"] = bool(import_request.start_instance)
    return replace(imported, import_metadata=metadata)


def _detect_explicit_difficulty(
    imported: ImportedChallenge,
    candidate: DiscoveredChallenge,
) -> str | None:
    for value in (
        imported.title,
        imported.description,
        candidate.text_block,
    ):
        match = EXPLICIT_DIFFICULTY_RE.search(value)
        if not match:
            continue
        normalized = match.group(1).strip().lower()
        if normalized == "very easy":
            return "easy"
        if normalized in {"easy", "medium", "hard"}:
            return normalized
    return None


def _infer_instance_required(
    imported: ImportedChallenge,
    candidate: DiscoveredChallenge,
    document: SourceDocument,
) -> bool:
    metadata = imported.import_metadata if isinstance(imported.import_metadata, dict) else {}
    if metadata.get("instance_access"):
        return True
    if str(metadata.get("start_instance_result") or "") in {
        "reused_current",
        "started",
        "started_after_timeout",
        "started_no_access",
    }:
        return True
    if imported.target_host:
        return False
    if imported.files:
        return False
    if candidate.challenge_id is None:
        return False
    return bool(INSTANCE_HINT_RE.search(imported.description))


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
