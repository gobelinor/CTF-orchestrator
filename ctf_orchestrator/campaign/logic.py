from __future__ import annotations

from dataclasses import replace
from datetime import datetime, UTC
from hashlib import sha1
from pathlib import Path
from typing import Any

from ..import_service import ImportedChallengeRecord
from ..workspace import _slugify
from .models import CampaignChallengeRecord, CampaignFilters, CampaignState


DIFFICULTY_RANK = {
    "easy": 0,
    "medium": 1,
    "hard": 3,
}


def campaign_dir_for_source(workspace_root: Path, source_label: str, board_source_key: str) -> Path:
    source_slug = _slugify(source_label.split("/")[-1] or source_label)
    return workspace_root / ".campaigns" / f"{source_slug}-{board_source_key.removeprefix('board-')}"


def campaign_name_for_source(source_label: str) -> str:
    tail = source_label.rstrip("/").split("/")[-1] or source_label
    return f"Campaign {tail}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def challenge_key_for_record(board_source_key: str, record: ImportedChallengeRecord) -> str:
    candidate = record.candidate
    if candidate.challenge_id is not None:
        return f"{board_source_key}:ctfd:{candidate.challenge_id}"

    if record.imported is not None:
        identity = "|".join(
            [
                record.imported.title,
                record.imported.category,
                str(record.imported.points),
                str(record.imported.solves),
            ]
        )
    else:
        identity = "|".join(
            [
                candidate.title,
                str(candidate.points),
                str(candidate.solves),
                candidate.text_block[:120],
            ]
        )
    digest = sha1(identity.encode("utf-8")).hexdigest()[:12]
    return f"{board_source_key}:text:{digest}"


def merge_import_records(
    state: CampaignState,
    records: list[ImportedChallengeRecord],
    *,
    filters: CampaignFilters,
) -> CampaignState:
    challenges = dict(state.challenges)

    for existing in challenges.values():
        if existing.status == "running":
            existing.status = "interrupted"

    for record in records:
        challenge_key = challenge_key_for_record(state.board_source_key, record)
        previous = challenges.get(challenge_key)
        if record.successful and record.imported is not None and record.payload is not None:
            metadata = record.payload.get("import_metadata", {})
            next_record = CampaignChallengeRecord(
                challenge_key=challenge_key,
                challenge_name=record.imported.title,
                challenge_payload=dict(record.payload),
                category=record.imported.category,
                explicit_difficulty=_maybe_str(metadata.get("explicit_difficulty")),
                points=record.imported.points,
                solves=record.imported.solves,
                instance_required=bool(metadata.get("instance_required")),
                instance_source=str(metadata.get("instance_source", "none")),
                start_instance_supported=bool(metadata.get("start_instance_supported")),
                status="pending",
                workspace=previous.workspace if previous is not None else "",
                campaign_attempts=previous.campaign_attempts if previous is not None else 0,
                previous_failures=previous.previous_failures if previous is not None else 0,
                last_summary=previous.last_summary if previous is not None else "",
                final_flag=previous.final_flag if previous is not None else None,
                import_error=None,
            )
            if previous is not None and previous.status in {"solved", "needs_human"} and not filters.retry_needs_human:
                next_record.status = previous.status
            elif previous is not None and previous.status == "solved":
                next_record.status = "solved"
            elif previous is not None and previous.status == "needs_human" and filters.retry_needs_human:
                next_record.status = "pending"
            elif previous is not None and previous.status == "interrupted":
                next_record.status = "pending"
            elif previous is not None and previous.status == "import_failed":
                next_record.status = "pending"
            challenges[challenge_key] = next_record
            continue

        message = record.error or "challenge import failed"
        if previous is not None:
            previous.import_error = message
            previous.last_summary = message
            previous.status = "import_failed"
            challenges[challenge_key] = previous
            continue
        challenges[challenge_key] = CampaignChallengeRecord(
            challenge_key=challenge_key,
            challenge_name=record.candidate.title,
            challenge_payload={},
            category=record.candidate.category,
            points=record.candidate.points,
            solves=record.candidate.solves,
            status="import_failed",
            last_summary=message,
            import_error=message,
        )

    return replace(state, challenges=challenges, updated_at=now_iso())


def apply_filters_and_priorities(state: CampaignState) -> CampaignState:
    for record in state.challenges.values():
        if record.status == "solved":
            record.priority_tuple = []
            record.priority_reason = "already solved"
            continue
        if record.status == "needs_human" and not state.filters.retry_needs_human:
            record.priority_tuple = []
            record.priority_reason = "already marked needs_human"
            continue
        if record.status == "import_failed":
            record.priority_tuple = []
            record.priority_reason = record.import_error or "import failed"
            continue

        if matches_filters(record, state.filters):
            record.status = "pending"
            priority_tuple = build_priority_tuple(record)
            record.priority_tuple = list(priority_tuple)
            record.priority_score = _priority_score(priority_tuple)
            record.priority_reason = build_priority_reason(record)
        else:
            record.status = "skipped"
            record.priority_tuple = []
            record.priority_score = 0
            record.priority_reason = "filtered out"

    pending = [record for record in state.challenges.values() if record.status == "pending"]
    if state.filters.max_challenges is not None and state.filters.max_challenges >= 0:
        keep = {
            record.challenge_key
            for record in sorted(pending, key=lambda item: tuple(item.priority_tuple))[: state.filters.max_challenges]
        }
        for record in pending:
            if record.challenge_key in keep:
                continue
            record.status = "skipped"
            record.priority_reason = "excluded by max_challenges"
            record.priority_tuple = []
            record.priority_score = 0

    return replace(state, updated_at=now_iso())


def matches_filters(record: CampaignChallengeRecord, filters: CampaignFilters) -> bool:
    if filters.categories:
        category = (record.category or "").lower()
        allowed = {item.strip().lower() for item in filters.categories if item.strip()}
        if category not in allowed:
            return False

    if filters.challenge_queries:
        title = record.challenge_name.lower()
        matched = any(
            query.strip().lower() == title or query.strip().lower() in title
            for query in filters.challenge_queries
            if query.strip()
        )
        if not matched:
            return False

    if filters.max_difficulty and record.explicit_difficulty:
        current_rank = difficulty_rank(record.explicit_difficulty)
        max_rank = difficulty_rank(filters.max_difficulty)
        if current_rank > max_rank:
            return False

    return True


def build_priority_tuple(record: CampaignChallengeRecord) -> tuple[int, int, int, int, int, str]:
    return (
        difficulty_rank(record.explicit_difficulty),
        record.points if record.points is not None else 999999,
        -(record.solves if record.solves is not None else -1),
        actionability_rank(record),
        record.previous_failures,
        record.challenge_name.lower(),
    )


def build_priority_reason(record: CampaignChallengeRecord) -> str:
    difficulty = record.explicit_difficulty or "unknown"
    points = record.points if record.points is not None else "?"
    solves = record.solves if record.solves is not None else "?"
    actionability = actionability_rank(record)
    return (
        f"difficulty={difficulty}, points={points}, solves={solves}, "
        f"actionability={actionability}, failures={record.previous_failures}"
    )


def difficulty_rank(value: str | None) -> int:
    if not value:
        return 2
    return DIFFICULTY_RANK.get(value.strip().lower(), 2)


def actionability_rank(record: CampaignChallengeRecord) -> int:
    payload = record.challenge_payload
    artifact_paths = list(payload.get("files", [])) + list(payload.get("artifact_paths", []))
    target_host = payload.get("target_host")
    if artifact_paths and not target_host:
        return 0
    if artifact_paths and target_host:
        return 1
    if target_host:
        return 2
    if payload:
        return 3
    return 4


def pending_queue(state: CampaignState) -> list[CampaignChallengeRecord]:
    pending = [record for record in state.challenges.values() if record.status == "pending"]
    return sorted(pending, key=lambda record: tuple(record.priority_tuple))


def _priority_score(priority_tuple: tuple[int, int, int, int, int, str]) -> int:
    difficulty, points, solves, actionability, failures, _ = priority_tuple
    return max(0, 1000 - (difficulty * 200) - points + solves - (actionability * 50) - (failures * 100))


def _maybe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
