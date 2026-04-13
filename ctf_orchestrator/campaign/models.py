from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CampaignFilters:
    categories: list[str] = field(default_factory=list)
    challenge_queries: list[str] = field(default_factory=list)
    max_difficulty: str | None = None
    max_challenges: int | None = None
    retry_needs_human: bool = False
    start_instance_when_needed: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CampaignFilters":
        return cls(
            categories=[str(item) for item in payload.get("categories", [])],
            challenge_queries=[str(item) for item in payload.get("challenge_queries", [])],
            max_difficulty=str(payload["max_difficulty"]) if payload.get("max_difficulty") else None,
            max_challenges=int(payload["max_challenges"]) if payload.get("max_challenges") is not None else None,
            retry_needs_human=bool(payload.get("retry_needs_human")),
            start_instance_when_needed=bool(payload.get("start_instance_when_needed")),
        )


@dataclass(frozen=True)
class CampaignCapacities:
    max_parallel_challenges: int = 1
    max_instance_challenges: int = 1

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CampaignCapacities":
        return cls(
            max_parallel_challenges=int(payload.get("max_parallel_challenges", 1)),
            max_instance_challenges=int(payload.get("max_instance_challenges", 1)),
        )


@dataclass
class CampaignChallengeRecord:
    challenge_key: str
    challenge_name: str
    challenge_payload: dict[str, Any]
    category: str | None = None
    explicit_difficulty: str | None = None
    points: int | None = None
    solves: int | None = None
    instance_required: bool = False
    instance_source: str = "none"
    start_instance_supported: bool = False
    status: str = "pending"
    priority_score: int = 0
    priority_tuple: list[Any] = field(default_factory=list)
    priority_reason: str = ""
    campaign_attempts: int = 0
    previous_failures: int = 0
    workspace: str = ""
    last_summary: str = ""
    final_flag: str | None = None
    import_error: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CampaignChallengeRecord":
        return cls(
            challenge_key=str(payload["challenge_key"]),
            challenge_name=str(payload["challenge_name"]),
            challenge_payload=dict(payload.get("challenge_payload", {})),
            category=str(payload["category"]) if payload.get("category") else None,
            explicit_difficulty=str(payload["explicit_difficulty"]) if payload.get("explicit_difficulty") else None,
            points=int(payload["points"]) if payload.get("points") is not None else None,
            solves=int(payload["solves"]) if payload.get("solves") is not None else None,
            instance_required=bool(payload.get("instance_required")),
            instance_source=str(payload.get("instance_source", "none")),
            start_instance_supported=bool(payload.get("start_instance_supported")),
            status=str(payload.get("status", "pending")),
            priority_score=int(payload.get("priority_score", 0)),
            priority_tuple=list(payload.get("priority_tuple", [])),
            priority_reason=str(payload.get("priority_reason", "")),
            campaign_attempts=int(payload.get("campaign_attempts", 0)),
            previous_failures=int(payload.get("previous_failures", 0)),
            workspace=str(payload.get("workspace", "")),
            last_summary=str(payload.get("last_summary", "")),
            final_flag=str(payload["final_flag"]) if payload.get("final_flag") else None,
            import_error=str(payload["import_error"]) if payload.get("import_error") else None,
        )


@dataclass
class CampaignState:
    campaign_key: str
    campaign_name: str
    source_label: str
    board_source_key: str
    filters: CampaignFilters
    capacities: CampaignCapacities
    challenges: dict[str, CampaignChallengeRecord] = field(default_factory=dict)
    started_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CampaignState":
        challenge_payload = payload.get("challenges", {})
        return cls(
            campaign_key=str(payload["campaign_key"]),
            campaign_name=str(payload["campaign_name"]),
            source_label=str(payload["source_label"]),
            board_source_key=str(payload["board_source_key"]),
            filters=CampaignFilters.from_payload(dict(payload.get("filters", {}))),
            capacities=CampaignCapacities.from_payload(dict(payload.get("capacities", {}))),
            challenges={
                str(key): CampaignChallengeRecord.from_payload(value)
                for key, value in challenge_payload.items()
                if isinstance(value, dict)
            },
            started_at=str(payload.get("started_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            completed_at=str(payload["completed_at"]) if payload.get("completed_at") else None,
        )

    def counts_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.challenges.values():
            counts[record.status] = counts.get(record.status, 0) + 1
        return counts
