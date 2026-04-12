from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import CampaignState


CAMPAIGN_STATE_FILE = "campaign.json"
CAMPAIGN_EVENTS_FILE = "events.jsonl"
CAMPAIGN_IMPORTED_BOARD_FILE = "imported-board.json"
CAMPAIGN_SUMMARY_FILE = "summary.md"


def load_campaign_state(campaign_dir: Path) -> CampaignState | None:
    state_path = campaign_dir / CAMPAIGN_STATE_FILE
    if not state_path.exists():
        return None
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    return CampaignState.from_payload(payload)


def save_campaign_state(campaign_dir: Path, state: CampaignState) -> None:
    campaign_dir.mkdir(parents=True, exist_ok=True)
    (campaign_dir / CAMPAIGN_STATE_FILE).write_text(
        json.dumps(asdict(state), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def append_campaign_event(campaign_dir: Path, event_type: str, payload: dict[str, Any]) -> None:
    campaign_dir.mkdir(parents=True, exist_ok=True)
    with (campaign_dir / CAMPAIGN_EVENTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": event_type, "payload": payload}, ensure_ascii=False) + "\n")


def save_imported_board_snapshot(campaign_dir: Path, snapshot: dict[str, Any]) -> None:
    campaign_dir.mkdir(parents=True, exist_ok=True)
    (campaign_dir / CAMPAIGN_IMPORTED_BOARD_FILE).write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def save_campaign_summary(campaign_dir: Path, markdown: str) -> None:
    campaign_dir.mkdir(parents=True, exist_ok=True)
    (campaign_dir / CAMPAIGN_SUMMARY_FILE).write_text(markdown, encoding="utf-8")
