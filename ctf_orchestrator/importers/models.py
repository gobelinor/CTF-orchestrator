from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ImportRequest:
    source: str | None
    input_file: Path | None
    output: Path | None
    use_stdout: bool
    review: bool
    selected_challenge: str | None
    list_only: bool
    session_cookie: str | None
    cookie_file: Path | None
    start_instance: bool = False


@dataclass(frozen=True)
class SourceDocument:
    source_type: str
    source_label: str
    raw_text: str
    urls: list[str] = field(default_factory=list)
    fetched_url: str | None = None
    raw_html: str | None = None


@dataclass(frozen=True)
class DiscoveredChallenge:
    title: str
    text_block: str
    challenge_id: int | None = None
    category: str | None = None
    points: int | None = None
    solves: int | None = None
    source_label: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ImportedChallenge:
    title: str
    description: str
    category: str
    target_host: str | None = None
    files: list[str] = field(default_factory=list)
    operator_hint: str | None = None
    points: int | None = None
    solves: int | None = None
    play_url: str | None = None
    references: list[str] = field(default_factory=list)
    source_snippet: str | None = None
    import_metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": self.title.strip(),
            "description": self.description.strip(),
        }
        if self.category:
            payload["category"] = self.category.strip()
        if self.target_host:
            payload["target_host"] = self.target_host.strip()
        if self.files:
            payload["files"] = [str(f).strip() for f in self.files if str(f).strip()]
        if self.operator_hint:
            payload["operator_hint"] = self.operator_hint.strip()
        if self.points is not None:
            payload["points"] = int(self.points)
        if self.solves is not None:
            payload["solves"] = int(self.solves)
        if self.play_url:
            payload["play_url"] = self.play_url.strip()
        if self.references:
            cleaned = [str(r).strip() for r in self.references if str(r).strip()]
            if cleaned:
                payload["references"] = cleaned
        if self.source_snippet:
            payload["source_snippet"] = self.source_snippet.rstrip()
        if self.import_metadata:
            payload["import_metadata"] = dict(self.import_metadata)
        if self.warnings:
            metadata = dict(payload.get("import_metadata", {}))
            metadata["warnings"] = list(self.warnings)
            payload["import_metadata"] = metadata
        return payload
