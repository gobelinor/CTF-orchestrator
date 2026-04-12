import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ctf_orchestrator.graph import WORKING_MEMORY_VERSION, _build_working_memory, _empty_working_memory
from ctf_orchestrator.workers import WorkerResult


def _result(**kwargs):
    base = {
        "backend": "mock",
        "status": "needs_retry",
        "summary": "x",
        "next_step": "y",
    }
    base.update(kwargs)
    return WorkerResult(**base)


class WorkingMemoryEvolutionTests(unittest.TestCase):
    def test_empty_memory_has_version_2(self):
        wm = _empty_working_memory()
        self.assertEqual(wm["memory_version"], WORKING_MEMORY_VERSION)
        self.assertEqual(wm["memory_version"], 2)
        for field in (
            "active_hypothesis",
            "tested_hypotheses",
            "rejected_hypotheses",
            "promising_leads",
            "stagnation_signals",
            "backend_performance",
            "reachable_targets",
            "useful_artifacts",
            "last_strategy_change_reason",
            "recommended_next_brief",
            "exploration_branches",
        ):
            self.assertIn(field, wm)

    def test_tested_hypothesis_appended(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            history = [{"attempt": 1, "backend": "mock", "status": "needs_retry", "summary": "s"}]
            result = _result(hypothesis="SQLi error based", hypothesis_result="rejected", confidence=0.2, branch_id="sqli-error")
            wm = _build_working_memory(workspace, history, result, previous_memory=None)
            self.assertEqual(len(wm["tested_hypotheses"]), 1)
            self.assertEqual(wm["tested_hypotheses"][0]["hypothesis"], "SQLi error based")
            self.assertIn("SQLi error based", wm["rejected_hypotheses"])

    def test_same_hypothesis_not_duplicated(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            history1 = [{"attempt": 1, "backend": "mock", "status": "needs_retry", "summary": "s"}]
            first = _build_working_memory(
                workspace, history1, _result(hypothesis="A", hypothesis_result="rejected"), previous_memory=None
            )
            history2 = history1 + [{"attempt": 2, "backend": "mock", "status": "needs_retry", "summary": "s"}]
            second = _build_working_memory(
                workspace, history2, _result(hypothesis="A", hypothesis_result="rejected"), previous_memory=first
            )
            self.assertEqual(len(second["tested_hypotheses"]), 1)

    def test_confirmed_high_confidence_becomes_promising_lead(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            history = [{"attempt": 1, "backend": "mock", "status": "needs_retry", "summary": "s"}]
            result = _result(hypothesis="RSA common modulus", hypothesis_result="confirmed", confidence=0.8)
            wm = _build_working_memory(workspace, history, result, previous_memory=None)
            self.assertIn("RSA common modulus", wm["promising_leads"])

    def test_backend_performance_accumulates(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            history = [{"attempt": 1, "backend": "mock", "status": "blocked", "summary": "s"}]
            wm = _build_working_memory(
                workspace,
                history,
                _result(status="blocked", confidence=0.1),
                previous_memory=None,
            )
            history.append({"attempt": 2, "backend": "mock", "status": "blocked", "summary": "s"})
            wm = _build_working_memory(
                workspace,
                history,
                _result(status="blocked", confidence=0.2),
                previous_memory=wm,
            )
            stats = wm["backend_performance"]["mock"]
            self.assertEqual(stats["attempts"], 2)
            self.assertEqual(stats["blocked"], 2)
            self.assertAlmostEqual(stats["avg_confidence"], 0.15, places=3)

    def test_branch_tracking(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            history = [{"attempt": 1, "backend": "mock", "status": "needs_retry", "summary": "s"}]
            wm = _build_working_memory(
                workspace,
                history,
                _result(hypothesis="H1", branch_id="branch-a"),
                previous_memory=None,
            )
            history.append({"attempt": 2, "backend": "mock", "status": "needs_retry", "summary": "s"})
            wm = _build_working_memory(
                workspace,
                history,
                _result(hypothesis="H1b", branch_id="branch-a"),
                previous_memory=wm,
            )
            self.assertEqual(len(wm["exploration_branches"]), 1)
            self.assertEqual(wm["exploration_branches"][0]["attempts"], 2)

    def test_artifacts_accumulate(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            history = [{"attempt": 1, "backend": "mock", "status": "needs_retry", "summary": "s"}]
            wm = _build_working_memory(
                workspace,
                history,
                _result(artifacts_produced=["exp.py"]),
                previous_memory=None,
            )
            history.append({"attempt": 2, "backend": "mock", "status": "needs_retry", "summary": "s"})
            wm = _build_working_memory(
                workspace,
                history,
                _result(artifacts_produced=["key.bin"]),
                previous_memory=wm,
            )
            self.assertIn("exp.py", wm["useful_artifacts"])
            self.assertIn("key.bin", wm["useful_artifacts"])


if __name__ == "__main__":
    unittest.main()
