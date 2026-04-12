import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ctf_orchestrator.orchestrator_service import (
    ChallengeRunRequest,
    ChallengeRunResult,
    run_challenge_parallel,
)


ROOT = Path(__file__).resolve().parents[1]


def _make_request(tmp_dir: Path) -> ChallengeRunRequest:
    return ChallengeRunRequest(
        challenge_payload={
            "challenge_name": "Demo",
            "challenge_text": "text",
            "category_hint": "web",
            "target_host": None,
            "artifact_paths": [],
            "challenge_metadata": {},
        },
        backend_sequence=["mock"],
        max_attempts=1,
        skills_root=ROOT / "skills",
        workspace_root=tmp_dir,
        thread_id="t",
    )


class ParallelTrajectoryTests(unittest.TestCase):
    def test_first_solved_wins(self):
        call_count = {"n": 0}

        def fake_run_challenge(request, event_sink=None):
            call_count["n"] += 1
            solved = call_count["n"] == 1
            return ChallengeRunResult(
                challenge_name=request.challenge_payload["challenge_name"],
                workspace=Path(request.workspace_root) / "demo",
                staged_artifacts=[],
                final_state={"solved": solved, "final_flag": "flag{x}" if solved else None, "attempts": 1},
            )

        with TemporaryDirectory() as tmp:
            request = _make_request(Path(tmp))
            with patch("ctf_orchestrator.orchestrator_service.run_challenge", side_effect=fake_run_challenge):
                result = run_challenge_parallel(request, n=3)
            self.assertTrue(result.final_state["solved"])
            self.assertEqual(result.final_state["final_flag"], "flag{x}")

    def test_no_trajectory_solves_returns_deepest(self):
        def fake_run_challenge(request, event_sink=None):
            attempts = 1 if request.thread_id.endswith("t0") else 3
            return ChallengeRunResult(
                challenge_name="D",
                workspace=Path(request.workspace_root) / "d",
                staged_artifacts=[],
                final_state={"solved": False, "final_flag": None, "attempts": attempts},
            )

        with TemporaryDirectory() as tmp:
            request = _make_request(Path(tmp))
            with patch("ctf_orchestrator.orchestrator_service.run_challenge", side_effect=fake_run_challenge):
                result = run_challenge_parallel(request, n=2)
            self.assertFalse(result.final_state["solved"])
            self.assertEqual(result.final_state["attempts"], 3)


if __name__ == "__main__":
    unittest.main()
