from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json

from ctf_orchestrator.graph import build_initial_state, build_orchestrator, load_resume_context
from ctf_orchestrator.workers import build_worker_pool


ROOT = Path(__file__).resolve().parents[1]


class GraphTest(unittest.TestCase):
    def test_graph_emits_route_and_attempt_events(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        graph = build_orchestrator(
            ROOT / "skills",
            build_worker_pool(["mock"]),
            event_handler=lambda event_type, payload: events.append((event_type, payload)),
        )
        initial_state = build_initial_state(
            challenge_name="Login Panel",
            challenge_text="Challenge web avec formulaire de login et aucune autre donnée.",
            workspace=ROOT,
            backend_sequence=["mock"],
            category_hint="web",
            max_attempts=2,
        )
        final_state = graph.invoke(
            initial_state,
            config={"configurable": {"thread_id": "test-events"}},
        )

        self.assertEqual(final_state["attempts"], 2)
        event_types = [event_type for event_type, _ in events]
        self.assertEqual(
            event_types,
            [
                "route_resolved",
                "attempt_completed",
                "attempt_analyzed",
                "decision_made",
                "attempt_completed",
                "attempt_analyzed",
                "decision_made",
            ],
        )
        self.assertEqual(events[0][1]["category"], "web")
        self.assertEqual(events[1][1]["attempt"], 1)

    def test_graph_persists_working_memory_and_handoff(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            graph = build_orchestrator(ROOT / "skills", build_worker_pool(["mock"]))
            initial_state = build_initial_state(
                challenge_name="Login Panel",
                challenge_text="Challenge web avec formulaire de login et aucune autre donnée.",
                workspace=workspace,
                backend_sequence=["mock"],
                category_hint="web",
                max_attempts=2,
            )
            final_state = graph.invoke(
                initial_state,
                config={"configurable": {"thread_id": "test-memory"}},
            )

            self.assertIn("working_memory", final_state)
            self.assertIn("key_commands", final_state["working_memory"])
            self.assertTrue((workspace / ".runs" / "working-memory.json").exists())

    def test_working_memory_is_isolated_per_challenge_workspace(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workspace_a = root / "challenge-a"
            workspace_b = root / "challenge-b"
            workspace_a.mkdir()
            workspace_b.mkdir()

            graph = build_orchestrator(ROOT / "skills", build_worker_pool(["mock"]))

            final_state_a = graph.invoke(
                build_initial_state(
                    challenge_name="Challenge A",
                    challenge_text="Challenge web avec formulaire de login et aucune autre donnée.",
                    workspace=workspace_a,
                    backend_sequence=["mock"],
                    category_hint="web",
                    max_attempts=2,
                ),
                config={"configurable": {"thread_id": "test-memory-a"}},
            )
            final_state_b = graph.invoke(
                build_initial_state(
                    challenge_name="Challenge B",
                    challenge_text="Challenge crypto avec texte et aucune autre donnée.",
                    workspace=workspace_b,
                    backend_sequence=["mock"],
                    category_hint="crypto",
                    max_attempts=2,
                ),
                config={"configurable": {"thread_id": "test-memory-b"}},
            )

            memory_path_a = workspace_a / ".runs" / "working-memory.json"
            memory_path_b = workspace_b / ".runs" / "working-memory.json"
            self.assertTrue(memory_path_a.exists())
            self.assertTrue(memory_path_b.exists())
            self.assertNotEqual(memory_path_a, memory_path_b)

            memory_a = json.loads(memory_path_a.read_text(encoding="utf-8"))
            memory_b = json.loads(memory_path_b.read_text(encoding="utf-8"))
            self.assertEqual(final_state_a["working_memory"], memory_a)
            self.assertEqual(final_state_b["working_memory"], memory_b)
            self.assertNotEqual(final_state_a["workspace"], final_state_b["workspace"])

    def test_graph_stops_after_max_attempts_with_mock_worker(self) -> None:
        graph = build_orchestrator(ROOT / "skills", build_worker_pool(["mock"]))
        initial_state = build_initial_state(
            challenge_name="Login Panel",
            challenge_text="Challenge web avec formulaire de login et aucune autre donnée.",
            workspace=ROOT,
            backend_sequence=["mock"],
            category_hint="web",
            max_attempts=2,
        )
        final_state = graph.invoke(
            initial_state,
            config={"configurable": {"thread_id": "test-max-attempts"}},
        )
        self.assertEqual(final_state["category"], "web")
        self.assertEqual(final_state["attempts"], 2)
        self.assertEqual(final_state["stop_reason"], "max_attempts_reached")
        self.assertFalse(final_state["solved"])

    def test_load_resume_context_recovers_prior_history_with_critical_review(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            graph = build_orchestrator(ROOT / "skills", build_worker_pool(["mock"]))
            first_final_state = graph.invoke(
                build_initial_state(
                    challenge_name="Login Panel",
                    challenge_text="Challenge web avec formulaire de login et aucune autre donnée.",
                    workspace=workspace,
                    backend_sequence=["mock"],
                    category_hint="web",
                    max_attempts=2,
                ),
                config={"configurable": {"thread_id": "test-resume-first"}},
            )

            resumed_history, resumed_memory = load_resume_context(workspace)

            self.assertEqual(len(resumed_history), 2)
            self.assertIn("resume_assessment", resumed_memory)
            self.assertTrue(resumed_memory["resume_assessment"]["restart_guidance"])

            resumed_initial_state = build_initial_state(
                challenge_name="Login Panel",
                challenge_text="Challenge web avec formulaire de login et aucune autre donnée.",
                workspace=workspace,
                backend_sequence=["mock"],
                category_hint="web",
                history=resumed_history,
                working_memory=resumed_memory,
                max_attempts=2,
            )
            self.assertEqual(len(resumed_initial_state["history"]), len(first_final_state["history"]))
            self.assertEqual(
                resumed_initial_state["working_memory"]["resume_assessment"],
                resumed_memory["resume_assessment"],
            )

    def test_graph_returns_flag_when_worker_finds_one(self) -> None:
        graph = build_orchestrator(ROOT / "skills", build_worker_pool(["mock"]))
        initial_state = build_initial_state(
            challenge_name="Inline Flag",
            challenge_text="L'énoncé contient déjà flag{inline-win}.",
            workspace=ROOT,
            backend_sequence=["mock"],
            max_attempts=1,
        )
        final_state = graph.invoke(
            initial_state,
            config={"configurable": {"thread_id": "test-inline-flag"}},
        )
        self.assertTrue(final_state["solved"])
        self.assertEqual(final_state["final_flag"], "flag{inline-win}")
        self.assertEqual(final_state["stop_reason"], "flag_found")


if __name__ == "__main__":
    unittest.main()
