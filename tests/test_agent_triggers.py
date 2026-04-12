import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ctf_orchestrator import orchestrator_service


ROOT = Path(__file__).resolve().parents[1]


class MemoryPersistTriggerTests(unittest.TestCase):
    def test_memory_persist_not_called_when_unsolved_and_no_request(self):
        calls = []

        def fake_persist(**kwargs):
            calls.append(kwargs)
            return None

        with patch.object(orchestrator_service, "persist_challenge_memory", side_effect=fake_persist):
            orchestrator_service.maybe_persist_memory(
                workspace=Path("/tmp"),
                challenge_name="X",
                challenge_text="Y",
                category_hint=None,
                target_host=None,
                final_state={"solved": False, "memory_persist_requested": False},
                skills_root=ROOT / "skills",
                workers={"mock": object()},
                backend_sequence=["mock"],
            )
        self.assertEqual(calls, [])

    def test_memory_persist_called_when_solved(self):
        calls = []

        def fake_persist(**kwargs):
            calls.append(kwargs)
            return {"persisted": True, "group_id": "ctf_writeups", "episode_name": "e", "summary": "s"}

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with patch.object(orchestrator_service, "persist_challenge_memory", side_effect=fake_persist):
                orchestrator_service.maybe_persist_memory(
                    workspace=workspace,
                    challenge_name="X",
                    challenge_text="Y",
                    category_hint=None,
                    target_host=None,
                    final_state={"solved": True, "final_flag": "flag{x}"},
                    skills_root=ROOT / "skills",
                    workers={"mock": object()},
                    backend_sequence=["mock"],
                )
            self.assertEqual(len(calls), 1)
            log_path = workspace / ".runs" / "memory" / "result.json"
            self.assertTrue(log_path.exists())
            self.assertEqual(json.loads(log_path.read_text())["persisted"], True)

    def test_memory_persist_called_when_requested_flag_only(self):
        calls = []

        def fake_persist(**kwargs):
            calls.append(kwargs)
            return None

        with patch.object(orchestrator_service, "persist_challenge_memory", side_effect=fake_persist):
            orchestrator_service.maybe_persist_memory(
                workspace=Path("/tmp"),
                challenge_name="X",
                challenge_text="Y",
                category_hint=None,
                target_host=None,
                final_state={"solved": False, "memory_persist_requested": True},
                skills_root=ROOT / "skills",
                workers={"mock": object()},
                backend_sequence=["mock"],
            )
        self.assertEqual(len(calls), 1)


class WriteupTriggerTests(unittest.TestCase):
    def test_writeup_not_written_when_unsolved(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = orchestrator_service.maybe_write_writeup(
                workspace=workspace,
                challenge_name="X",
                challenge_text="Y",
                category_hint=None,
                target_host=None,
                final_state={"solved": False, "history": [], "final_summary": "nope"},
            )
            self.assertIsNone(result)
            self.assertFalse((workspace / "writeup.md").exists())

    def test_writeup_written_when_solved_fallback_markdown(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = orchestrator_service.maybe_write_writeup(
                workspace=workspace,
                challenge_name="Inline",
                challenge_text="nothing",
                category_hint="web",
                target_host=None,
                final_state={
                    "solved": True,
                    "final_flag": "flag{ok}",
                    "history": [],
                    "final_summary": "solved",
                    "latest_worker_output": {},
                },
            )
            self.assertIsNotNone(result)
            self.assertIn("flag{ok}", (workspace / "writeup.md").read_text())


if __name__ == "__main__":
    unittest.main()
