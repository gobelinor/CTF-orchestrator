from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
import threading
import time
import unittest
from unittest.mock import patch

from ctf_orchestrator.campaign.logic import apply_filters_and_priorities
from ctf_orchestrator.campaign.models import CampaignCapacities, CampaignFilters, CampaignState
from ctf_orchestrator.campaign import campaign_dir_for_source, load_campaign_state
from ctf_orchestrator.import_service import BoardImportContext, ImportedChallengeRecord
from ctf_orchestrator.importers.models import DiscoveredChallenge, ImportedChallenge, ImportRequest, SourceDocument
from ctf_orchestrator.orchestrator_service import ChallengeRunResult
from ctf_orchestrator.supervisor import SupervisorRunRequest, run_supervisor


def _board_context() -> BoardImportContext:
    return BoardImportContext(
        import_request=ImportRequest(
            source="https://ctf.example.com/challenges",
            input_file=None,
            output=None,
            use_stdout=False,
            review=False,
            selected_challenge=None,
            list_only=False,
            session_cookie=None,
            cookie_file=None,
            start_instance=False,
        ),
        document=SourceDocument(
            source_type="url_html",
            source_label="https://ctf.example.com/challenges",
            raw_text="",
            fetched_url="https://ctf.example.com/challenges",
        ),
        candidates=[],
        board_source_key="board-demo",
        source_label="https://ctf.example.com/challenges",
    )


def _imported_record(
    *,
    title: str,
    category: str,
    points: int,
    solves: int,
    difficulty: str | None = None,
    instance_required: bool = False,
    start_instance_supported: bool = False,
    target_host: str | None = None,
    challenge_id: int | None = None,
) -> ImportedChallengeRecord:
    candidate = DiscoveredChallenge(
        title=title,
        text_block=f"{title} {points} pts · {solves} Solves",
        challenge_id=challenge_id,
        category=category,
        points=points,
        solves=solves,
        source_label="https://ctf.example.com/challenges",
    )
    metadata = {
        "board_source_key": "board-demo",
        "instance_required": instance_required,
        "instance_source": "ctfd_container" if instance_required else "none",
        "start_instance_supported": start_instance_supported,
    }
    if difficulty:
        metadata["explicit_difficulty"] = difficulty
    imported = ImportedChallenge(
        title=title,
        description=f"{title} description",
        category=category,
        target_host=target_host,
        points=points,
        solves=solves,
        import_metadata=metadata,
    )
    return ImportedChallengeRecord(candidate=candidate, imported=imported, payload=imported.to_payload())


class CampaignLogicTest(unittest.TestCase):
    def test_apply_filters_and_priorities_respects_category_title_and_max_difficulty(self) -> None:
        state = CampaignState(
            campaign_key="board-demo",
            campaign_name="Campaign demo",
            source_label="https://ctf.example.com/challenges",
            board_source_key="board-demo",
            filters=CampaignFilters(
                categories=["crypto"],
                challenge_queries=["Noise"],
                max_difficulty="medium",
            ),
            capacities=CampaignCapacities(max_parallel_challenges=2, max_instance_challenges=1),
            challenges={
                "a": _record_from_import(
                    _imported_record(
                        title="Noise Cheap",
                        category="crypto",
                        points=90,
                        solves=337,
                        difficulty="easy",
                    ),
                    "a",
                ),
                "b": _record_from_import(
                    _imported_record(
                        title="Forbidden Fruit",
                        category="crypto",
                        points=150,
                        solves=754,
                        difficulty="hard",
                    ),
                    "b",
                ),
                "c": _record_from_import(
                    _imported_record(
                        title="Login Panel",
                        category="web",
                        points=50,
                        solves=800,
                        difficulty="easy",
                    ),
                    "c",
                ),
            },
        )

        filtered = apply_filters_and_priorities(state)

        self.assertEqual(filtered.challenges["a"].status, "pending")
        self.assertEqual(filtered.challenges["b"].status, "skipped")
        self.assertEqual(filtered.challenges["c"].status, "skipped")
        self.assertTrue(filtered.challenges["a"].priority_reason.startswith("difficulty=easy"))


class SupervisorRunTest(unittest.TestCase):
    def test_run_supervisor_respects_parallel_and_instance_capacities(self) -> None:
        context = _board_context()
        records = [
            _imported_record(
                title="Crypto Warmup",
                category="crypto",
                points=10,
                solves=500,
                difficulty="easy",
                instance_required=True,
                start_instance_supported=True,
                target_host="inst1.example:31337",
                challenge_id=1,
            ),
            _imported_record(
                title="Offline Forensics",
                category="forensics",
                points=20,
                solves=400,
                difficulty="easy",
            ),
            _imported_record(
                title="Second Instance",
                category="web",
                points=30,
                solves=300,
                difficulty="easy",
                instance_required=True,
                start_instance_supported=True,
                target_host="inst2.example:31337",
                challenge_id=2,
            ),
        ]
        context = replace(context, candidates=[record.candidate for record in records])

        active_total = 0
        active_instance = 0
        max_total = 0
        max_instance = 0
        lock = threading.Lock()

        def fake_run_challenge(request, event_sink=None):
            nonlocal active_total, active_instance, max_total, max_instance
            is_instance = bool(request.challenge_payload.get("import_metadata", {}).get("instance_required"))
            with lock:
                active_total += 1
                if is_instance:
                    active_instance += 1
                max_total = max(max_total, active_total)
                max_instance = max(max_instance, active_instance)
            time.sleep(0.05)
            if event_sink is not None:
                event_sink(
                    "attempt_completed",
                    {
                        "attempt": 1,
                        "backend": "mock",
                        "status": "solved",
                        "summary": "done",
                        "next_step": "submit",
                        "commands": [],
                    },
                )
            with lock:
                active_total -= 1
                if is_instance:
                    active_instance -= 1
            workspace = Path("/tmp") / request.thread_id
            return ChallengeRunResult(
                challenge_name=str(request.challenge_payload.get("title", "challenge")),
                workspace=workspace,
                staged_artifacts=[],
                final_state={"solved": True, "final_flag": "flag{ok}", "final_summary": "done"},
            )

        with TemporaryDirectory() as tmp_dir, patch(
            "ctf_orchestrator.supervisor.load_board_context", return_value=context
        ), patch(
            "ctf_orchestrator.supervisor.import_selected_candidates", return_value=records
        ), patch(
            "ctf_orchestrator.supervisor.run_challenge", side_effect=fake_run_challenge
        ):
            result = run_supervisor(
                SupervisorRunRequest(
                    import_request=context.import_request,
                    workspace_root=Path(tmp_dir),
                    skills_root=Path("skills"),
                    backend_sequence=["mock"],
                    max_attempts=1,
                    categories=[],
                    challenge_queries=[],
                    max_difficulty=None,
                    max_challenges=None,
                    max_parallel_challenges=2,
                    max_instance_challenges=1,
                    start_instance_when_needed=False,
                )
            )

        self.assertEqual(result.state.counts_by_status().get("solved"), 3)
        self.assertLessEqual(max_total, 2)
        self.assertLessEqual(max_instance, 1)

    def test_run_supervisor_skips_needs_human_until_retry_requested(self) -> None:
        context = _board_context()
        record = _imported_record(
            title="Sticky Challenge",
            category="crypto",
            points=50,
            solves=100,
            difficulty="medium",
        )
        context = replace(context, candidates=[record.candidate])
        calls = {"count": 0}

        def unsolved_run(request, event_sink=None):
            calls["count"] += 1
            return ChallengeRunResult(
                challenge_name="Sticky Challenge",
                workspace=Path("/tmp/sticky"),
                staged_artifacts=[],
                final_state={"solved": False, "final_flag": None, "final_summary": "too hard"},
            )

        with TemporaryDirectory() as tmp_dir, patch(
            "ctf_orchestrator.supervisor.load_board_context", return_value=context
        ), patch(
            "ctf_orchestrator.supervisor.import_selected_candidates", return_value=[record]
        ), patch(
            "ctf_orchestrator.supervisor.run_challenge", side_effect=unsolved_run
        ):
            workspace_root = Path(tmp_dir)
            first = run_supervisor(
                SupervisorRunRequest(
                    import_request=context.import_request,
                    workspace_root=workspace_root,
                    skills_root=Path("skills"),
                    backend_sequence=["mock"],
                    max_attempts=1,
                    categories=[],
                    challenge_queries=[],
                    max_difficulty=None,
                    max_challenges=None,
                    max_parallel_challenges=1,
                    max_instance_challenges=1,
                )
            )
            second = run_supervisor(
                SupervisorRunRequest(
                    import_request=context.import_request,
                    workspace_root=workspace_root,
                    skills_root=Path("skills"),
                    backend_sequence=["mock"],
                    max_attempts=1,
                    categories=[],
                    challenge_queries=[],
                    max_difficulty=None,
                    max_challenges=None,
                    max_parallel_challenges=1,
                    max_instance_challenges=1,
                )
            )

        self.assertEqual(calls["count"], 1)
        self.assertEqual(first.state.counts_by_status().get("needs_human"), 1)
        self.assertEqual(second.state.counts_by_status().get("needs_human"), 1)

    def test_run_supervisor_retries_needs_human_with_flag(self) -> None:
        context = _board_context()
        record = _imported_record(
            title="Retry Challenge",
            category="crypto",
            points=50,
            solves=100,
            difficulty="medium",
        )
        context = replace(context, candidates=[record.candidate])
        calls = {"count": 0}

        def varying_run(request, event_sink=None):
            calls["count"] += 1
            solved = calls["count"] > 1
            return ChallengeRunResult(
                challenge_name="Retry Challenge",
                workspace=Path("/tmp/retry"),
                staged_artifacts=[],
                final_state={
                    "solved": solved,
                    "final_flag": "flag{retry}" if solved else None,
                    "final_summary": "done" if solved else "blocked",
                },
            )

        with TemporaryDirectory() as tmp_dir, patch(
            "ctf_orchestrator.supervisor.load_board_context", return_value=context
        ), patch(
            "ctf_orchestrator.supervisor.import_selected_candidates", return_value=[record]
        ), patch(
            "ctf_orchestrator.supervisor.run_challenge", side_effect=varying_run
        ):
            workspace_root = Path(tmp_dir)
            run_supervisor(
                SupervisorRunRequest(
                    import_request=context.import_request,
                    workspace_root=workspace_root,
                    skills_root=Path("skills"),
                    backend_sequence=["mock"],
                    max_attempts=1,
                    categories=[],
                    challenge_queries=[],
                    max_difficulty=None,
                    max_challenges=None,
                    max_parallel_challenges=1,
                    max_instance_challenges=1,
                )
            )
            retried = run_supervisor(
                SupervisorRunRequest(
                    import_request=context.import_request,
                    workspace_root=workspace_root,
                    skills_root=Path("skills"),
                    backend_sequence=["mock"],
                    max_attempts=1,
                    categories=[],
                    challenge_queries=[],
                    max_difficulty=None,
                    max_challenges=None,
                    max_parallel_challenges=1,
                    max_instance_challenges=1,
                    retry_needs_human=True,
                )
            )

        self.assertEqual(calls["count"], 2)
        self.assertEqual(retried.state.counts_by_status().get("solved"), 1)

    def test_run_supervisor_marks_running_challenge_interrupted_when_start_event_raises(self) -> None:
        context = _board_context()
        record = _imported_record(
            title="Accela Signal",
            category="rf",
            points=579,
            solves=12,
            instance_required=True,
            start_instance_supported=True,
            target_host="espilon.net:31337",
            challenge_id=30,
        )
        context = replace(context, candidates=[record.candidate])

        def raising_sink(event_type, payload):
            if event_type == "campaign_challenge_started":
                raise RuntimeError("sink exploded")

        with TemporaryDirectory() as tmp_dir, patch(
            "ctf_orchestrator.supervisor.load_board_context", return_value=context
        ), patch(
            "ctf_orchestrator.supervisor.import_selected_candidates", return_value=[record]
        ):
            with self.assertRaises(RuntimeError):
                run_supervisor(
                    SupervisorRunRequest(
                        import_request=context.import_request,
                        workspace_root=Path(tmp_dir),
                        skills_root=Path("skills"),
                        backend_sequence=["mock"],
                        max_attempts=1,
                        categories=[],
                        challenge_queries=[],
                        max_difficulty=None,
                        max_challenges=None,
                        max_parallel_challenges=1,
                        max_instance_challenges=1,
                        start_instance_when_needed=False,
                    ),
                    event_sink=raising_sink,
                )

            campaign_dir = campaign_dir_for_source(Path(tmp_dir), context.source_label, context.board_source_key)
            saved_state = load_campaign_state(campaign_dir)
            summary_exists = (campaign_dir / "summary.md").exists()

        self.assertIsNotNone(saved_state)
        saved_records = list(saved_state.challenges.values())
        self.assertEqual(len(saved_records), 1)
        self.assertEqual(saved_records[0].status, "interrupted")
        self.assertEqual(saved_records[0].previous_failures, 1)
        self.assertEqual(saved_records[0].last_summary, "supervisor exited before challenge completion")
        self.assertTrue(summary_exists)


def _record_from_import(record: ImportedChallengeRecord, challenge_key: str):
    payload = record.payload or {}
    metadata = payload.get("import_metadata", {})
    from ctf_orchestrator.campaign.models import CampaignChallengeRecord

    return CampaignChallengeRecord(
        challenge_key=challenge_key,
        challenge_name=record.imported.title if record.imported else record.candidate.title,
        challenge_payload=payload,
        category=record.imported.category if record.imported else record.candidate.category,
        explicit_difficulty=metadata.get("explicit_difficulty"),
        points=record.imported.points if record.imported else record.candidate.points,
        solves=record.imported.solves if record.imported else record.candidate.solves,
        instance_required=bool(metadata.get("instance_required")),
        instance_source=str(metadata.get("instance_source", "none")),
        start_instance_supported=bool(metadata.get("start_instance_supported")),
    )


if __name__ == "__main__":
    unittest.main()
