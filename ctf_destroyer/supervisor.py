from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .campaign import (
    CampaignCapacities,
    CampaignFilters,
    CampaignState,
    append_campaign_event,
    apply_filters_and_priorities,
    campaign_dir_for_source,
    campaign_name_for_source,
    load_campaign_state,
    pending_queue,
    save_campaign_state,
    save_campaign_summary,
    save_imported_board_snapshot,
)
from .campaign.logic import merge_import_records, now_iso
from .import_service import (
    BoardImportContext,
    import_candidate,
    import_selected_candidates,
    load_board_context,
    validate_instance_access,
)
from .importers import ImportRequest
from .importers.sources import resolve_cookie_header
from .orchestrator_service import ChallengeRunRequest, run_challenge


EventSink = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class SupervisorRunRequest:
    import_request: ImportRequest
    workspace_root: Path
    skills_root: Path
    backend_sequence: list[str]
    max_attempts: int
    categories: list[str]
    challenge_queries: list[str]
    max_difficulty: str | None
    max_challenges: int | None
    max_parallel_challenges: int
    max_instance_challenges: int
    retry_needs_human: bool = False
    start_instance_when_needed: bool = False


@dataclass(frozen=True)
class SupervisorRunResult:
    campaign_dir: Path
    state: CampaignState


def run_supervisor(
    request: SupervisorRunRequest,
    event_sink: EventSink | None = None,
) -> SupervisorRunResult:
    board_request = _build_board_import_request(request.import_request)
    context = load_board_context(board_request)
    filters = CampaignFilters(
        categories=list(request.categories),
        challenge_queries=list(request.challenge_queries),
        max_difficulty=request.max_difficulty,
        max_challenges=request.max_challenges,
        retry_needs_human=request.retry_needs_human,
        start_instance_when_needed=request.start_instance_when_needed,
    )
    capacities = CampaignCapacities(
        max_parallel_challenges=request.max_parallel_challenges,
        max_instance_challenges=request.max_instance_challenges,
    )
    campaign_dir = campaign_dir_for_source(request.workspace_root, context.source_label, context.board_source_key)
    state = load_campaign_state(campaign_dir) or CampaignState(
        campaign_key=context.board_source_key,
        campaign_name=campaign_name_for_source(context.source_label),
        source_label=context.source_label,
        board_source_key=context.board_source_key,
        filters=filters,
        capacities=capacities,
        started_at=now_iso(),
        updated_at=now_iso(),
    )
    state.filters = filters
    state.capacities = capacities
    state.updated_at = now_iso()

    def emit(event_type: str, payload: dict[str, Any]) -> None:
        append_campaign_event(campaign_dir, event_type, payload)
        if event_sink is not None:
            event_sink(event_type, payload)

    emit(
        "campaign_started",
        {
            "campaign_name": state.campaign_name,
            "campaign_dir": str(campaign_dir),
            "source_label": state.source_label,
            "filters": filters.to_payload(),
            "capacities": capacities.to_payload(),
        },
    )

    imported_records = import_selected_candidates(context, start_instance=False)
    save_imported_board_snapshot(
        campaign_dir,
        {
            "board_source_key": context.board_source_key,
            "source_label": context.source_label,
            "records": [
                {
                    "title": record.candidate.title,
                    "challenge_id": record.candidate.challenge_id,
                    "error": record.error,
                    "payload": record.payload,
                }
                for record in imported_records
            ],
        },
    )
    state = merge_import_records(state, imported_records, filters=filters)
    state = apply_filters_and_priorities(state)
    save_campaign_state(campaign_dir, state)
    emit(
        "campaign_import_completed",
        {
            "campaign_name": state.campaign_name,
            "discovered": len(imported_records),
            "eligible": len([item for item in state.challenges.values() if item.status == "pending"]),
            "skipped": len([item for item in state.challenges.values() if item.status == "skipped"]),
            "import_failed": len([item for item in state.challenges.values() if item.status == "import_failed"]),
        },
    )

    candidate_by_key = {
        record_key: record.candidate
        for record in imported_records
        for record_key in [state_key_for_candidate(state, record)]
    }
    active: dict[Future[Any], str] = {}
    run_completed = False
    try:
        with ThreadPoolExecutor(max_workers=max(1, request.max_parallel_challenges)) as executor:
            while True:
                _launch_available_challenges(
                    state=state,
                    context=context,
                    request=request,
                    executor=executor,
                    active=active,
                    candidate_by_key=candidate_by_key,
                    emit=emit,
                    campaign_dir=campaign_dir,
                )
                if not active:
                    break

                done, _ = wait(active.keys(), timeout=0.2, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    challenge_key = active.pop(future)
                    record = state.challenges[challenge_key]
                    try:
                        result = future.result()
                    except Exception as exc:
                        record.status = "interrupted"
                        record.previous_failures += 1
                        record.last_summary = str(exc)
                        emit(
                            "campaign_challenge_completed",
                            {
                                "challenge_name": record.challenge_name,
                                "challenge_key": record.challenge_key,
                                "status": record.status,
                                "summary": record.last_summary,
                            },
                        )
                    else:
                        record.workspace = str(result.workspace)
                        final_state = result.final_state
                        record.challenge_payload = dict(record.challenge_payload)
                        if final_state.get("solved"):
                            record.status = "solved"
                            record.final_flag = str(final_state.get("final_flag") or "") or None
                        else:
                            record.status = "needs_human"
                            record.previous_failures += 1
                        record.last_summary = str(final_state.get("final_summary", ""))
                        emit(
                            "campaign_challenge_completed",
                            {
                                "challenge_name": record.challenge_name,
                                "challenge_key": record.challenge_key,
                                "status": record.status,
                                "summary": record.last_summary,
                                "final_flag": record.final_flag,
                            },
                        )
                    state.updated_at = now_iso()
                    save_campaign_state(campaign_dir, state)

                if not pending_queue(state) and not active:
                    break
        run_completed = True
    finally:
        interrupted = _interrupt_running_challenges(
            state=state,
            emit=emit,
            campaign_dir=campaign_dir,
            summary="supervisor exited before challenge completion",
        )
        if interrupted or not run_completed:
            state.updated_at = now_iso()
            save_campaign_state(campaign_dir, state)
            save_campaign_summary(campaign_dir, render_campaign_summary(state))

    state.completed_at = now_iso()
    state.updated_at = state.completed_at
    save_campaign_state(campaign_dir, state)
    summary = render_campaign_summary(state)
    save_campaign_summary(campaign_dir, summary)
    emit(
        "campaign_completed",
        {
            "campaign_name": state.campaign_name,
            "counts": state.counts_by_status(),
            "summary_path": str(campaign_dir / "summary.md"),
        },
    )
    return SupervisorRunResult(campaign_dir=campaign_dir, state=state)


def render_campaign_summary(state: CampaignState) -> str:
    counts = state.counts_by_status()
    lines = [
        "# Campaign Summary",
        "",
        f"**Campaign:** {state.campaign_name}",
        f"**Source:** `{state.source_label}`",
        "",
        "## Counts",
        "",
    ]
    for status in sorted(counts):
        lines.append(f"- `{status}`: {counts[status]}")
    lines.extend(
        [
            "",
            "## Challenges",
            "",
        ]
    )
    for record in sorted(state.challenges.values(), key=lambda item: (item.status, item.challenge_name.lower())):
        summary = record.last_summary or record.priority_reason or ""
        lines.append(f"- `{record.status}` {record.challenge_name}: {summary}".rstrip())
    return "\n".join(lines).strip() + "\n"


def _launch_available_challenges(
    *,
    state: CampaignState,
    context: BoardImportContext,
    request: SupervisorRunRequest,
    executor: ThreadPoolExecutor,
    active: dict[Future[Any], str],
    candidate_by_key: dict[str, Any],
    emit: EventSink,
    campaign_dir: Path,
) -> None:
    queue = pending_queue(state)
    if not queue:
        return

    for record in queue:
        if len(active) >= state.capacities.max_parallel_challenges:
            break
        if record.instance_required and _active_instance_count(active, state) >= state.capacities.max_instance_challenges:
            continue

        if record.instance_required and not record.challenge_payload.get("target_host"):
            if not request.start_instance_when_needed:
                record.status = "import_failed"
                record.import_error = "instance startup required; rerun with --start-instance-when-needed"
                record.last_summary = record.import_error
                emit(
                    "campaign_challenge_completed",
                    {
                        "challenge_name": record.challenge_name,
                        "challenge_key": record.challenge_key,
                        "status": record.status,
                        "summary": record.last_summary,
                    },
                )
                save_campaign_state(campaign_dir, state)
                continue
            if not record.start_instance_supported:
                record.status = "import_failed"
                record.import_error = "instance startup is required but not supported by the imported source"
                record.last_summary = record.import_error
                emit(
                    "campaign_challenge_completed",
                    {
                        "challenge_name": record.challenge_name,
                        "challenge_key": record.challenge_key,
                        "status": record.status,
                        "summary": record.last_summary,
                    },
                )
                save_campaign_state(campaign_dir, state)
                continue

        if (
            request.start_instance_when_needed
            and record.instance_required
            and not record.challenge_payload.get("target_host")
            and record.start_instance_supported
        ):
            candidate = candidate_by_key.get(record.challenge_key)
            if candidate is None:
                record.status = "import_failed"
                record.import_error = "missing candidate metadata for instance startup"
                record.last_summary = record.import_error
                save_campaign_state(campaign_dir, state)
                continue
            try:
                imported = import_candidate(context, candidate, start_instance=True)
                payload = imported.to_payload()
                instance_error = validate_instance_access(
                    ImportRequest(
                        source=context.import_request.source,
                        input_file=context.import_request.input_file,
                        output=None,
                        use_stdout=False,
                        review=context.import_request.review,
                        selected_challenge=None,
                        list_only=False,
                        session_cookie=context.import_request.session_cookie,
                        cookie_file=context.import_request.cookie_file,
                        start_instance=True,
                    ),
                    imported,
                )
                if instance_error:
                    raise RuntimeError(instance_error)
            except Exception as exc:
                record.status = "import_failed"
                record.import_error = str(exc)
                record.last_summary = str(exc)
                emit(
                    "campaign_challenge_completed",
                    {
                        "challenge_name": record.challenge_name,
                        "challenge_key": record.challenge_key,
                        "status": record.status,
                        "summary": record.last_summary,
                    },
                )
                save_campaign_state(campaign_dir, state)
                continue
            record.challenge_payload = payload

        record.status = "running"
        record.campaign_attempts += 1
        state.updated_at = now_iso()
        save_campaign_state(campaign_dir, state)
        emit(
            "campaign_challenge_started",
            {
                "challenge_name": record.challenge_name,
                "challenge_key": record.challenge_key,
                "category": record.category,
                "priority_reason": record.priority_reason,
                "instance_required": record.instance_required,
            },
        )
        future = executor.submit(
            run_challenge,
            ChallengeRunRequest(
                challenge_payload=dict(record.challenge_payload),
                backend_sequence=request.backend_sequence,
                max_attempts=request.max_attempts,
                skills_root=request.skills_root,
                workspace_root=request.workspace_root,
                artifact_cookie_header=resolve_cookie_header(context.import_request),
                thread_id=record.challenge_key,
                source_root=_resolve_source_root(context),
            ),
            _build_challenge_event_sink(emit, record.challenge_key),
        )
        active[future] = record.challenge_key


def _interrupt_running_challenges(
    *,
    state: CampaignState,
    emit: EventSink,
    campaign_dir: Path,
    summary: str,
) -> int:
    interrupted = 0
    for record in state.challenges.values():
        if record.status != "running":
            continue
        record.status = "interrupted"
        record.previous_failures += 1
        if not record.last_summary:
            record.last_summary = summary
        emit(
            "campaign_challenge_completed",
            {
                "challenge_name": record.challenge_name,
                "challenge_key": record.challenge_key,
                "status": record.status,
                "summary": record.last_summary,
            },
        )
        interrupted += 1
    if interrupted:
        state.updated_at = now_iso()
        save_campaign_state(campaign_dir, state)
    return interrupted


def _active_instance_count(active: dict[Future[Any], str], state: CampaignState) -> int:
    count = 0
    for challenge_key in active.values():
        record = state.challenges[challenge_key]
        if record.instance_required:
            count += 1
    return count


def _build_board_import_request(import_request: ImportRequest) -> ImportRequest:
    return ImportRequest(
        source=import_request.source,
        input_file=import_request.input_file,
        output=None,
        use_stdout=False,
        review=import_request.review,
        selected_challenge=None,
        list_only=False,
        session_cookie=import_request.session_cookie,
        cookie_file=import_request.cookie_file,
        start_instance=False,
    )


def _build_challenge_event_sink(emit: EventSink, challenge_key: str) -> EventSink:
    def handler(event_type: str, payload: dict[str, Any]) -> None:
        emit(event_type, {"challenge_key": challenge_key, **payload})

    return handler


def _resolve_source_root(context: BoardImportContext) -> Path:
    request = context.import_request
    if request.input_file is not None:
        return request.input_file.parent
    return Path.cwd()


def state_key_for_candidate(state: CampaignState, record: Any) -> str:
    from .campaign.logic import challenge_key_for_record

    return challenge_key_for_record(state.board_source_key, record)
