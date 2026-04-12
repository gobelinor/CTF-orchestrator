import unittest

from ctf_orchestrator.graph import (
    DECISION_NEEDS_HUMAN,
    DECISION_REASSESS_CATEGORY,
    DECISION_REQUEST_MEMORY,
    DECISION_REQUEST_WRITEUP,
    DECISION_RETRY_REFRAMED,
    DECISION_RETRY_SAME,
    DECISION_STOP,
    DECISION_SWITCH_BACKEND,
    _build_attempt_brief,
    _build_stop_summary,
    _choose_decision,
    _detect_stagnation_signals,
    _select_next_backend_index,
)
from ctf_orchestrator.workers import WorkerResult


def _make_result(**kwargs):
    base = {
        "backend": "mock",
        "status": "needs_retry",
        "summary": "trying sqli",
        "next_step": "try another param",
        "flag": None,
        "evidence": [],
        "commands": [],
    }
    base.update(kwargs)
    return WorkerResult(**base)


def _decide(state_overrides=None, latest=None, memory=None, history=None, stagnation=None):
    state = {
        "backend_sequence": ["mock", "claude"],
        "backend_index": 0,
        "active_backend": "mock",
    }
    state.update(state_overrides or {})
    return _choose_decision(
        state=state,
        latest=latest or _make_result(),
        working_memory=memory or {},
        history=history or [],
        attempts=state.get("attempts", 1),
        max_attempts=state.get("max_attempts", 4),
        stagnation=stagnation or [],
    )


class DecideNextTests(unittest.TestCase):
    def test_flag_triggers_writeup_then_memory_then_stop(self):
        latest = _make_result(status="solved", flag="flag{test}")
        decision, reason = _decide(latest=latest)
        self.assertEqual(decision, DECISION_REQUEST_WRITEUP)
        self.assertEqual(reason, "flag_found")

        decision, reason = _decide(
            state_overrides={"writeup_requested": True},
            latest=latest,
        )
        self.assertEqual(decision, DECISION_REQUEST_MEMORY)

        decision, reason = _decide(
            state_overrides={"writeup_requested": True, "memory_persist_requested": True},
            latest=latest,
        )
        self.assertEqual(decision, DECISION_STOP)

    def test_worker_recommended_action_is_honored(self):
        latest = _make_result(recommended_action=DECISION_SWITCH_BACKEND, status="blocked")
        decision, reason = _decide(latest=latest)
        self.assertEqual(decision, DECISION_SWITCH_BACKEND)
        self.assertTrue(reason.startswith("worker_recommended"))

    def test_worker_needs_human_flag(self):
        latest = _make_result(needs_human=True, status="blocked")
        decision, _ = _decide(latest=latest)
        self.assertEqual(decision, DECISION_NEEDS_HUMAN)

    def test_wrong_category_failure_triggers_reassess(self):
        latest = _make_result(failure_reason="wrong_category", status="blocked")
        decision, _ = _decide(latest=latest)
        self.assertEqual(decision, DECISION_REASSESS_CATEGORY)

    def test_hypothesis_loop_stagnation_forces_reframed(self):
        latest = _make_result(status="needs_retry")
        decision, reason = _decide(
            latest=latest,
            stagnation=["hypothesis_loop"],
            state_overrides={"attempts": 2, "max_attempts": 4},
        )
        self.assertEqual(decision, DECISION_RETRY_REFRAMED)
        self.assertIn("stagnation", reason)

    def test_three_blocked_stagnation_forces_switch(self):
        latest = _make_result(status="blocked")
        decision, _ = _decide(
            latest=latest,
            stagnation=["three_consecutive_blocked"],
            state_overrides={"attempts": 3, "max_attempts": 6},
        )
        self.assertEqual(decision, DECISION_SWITCH_BACKEND)

    def test_stagnation_at_budget_exhausted_forces_stop(self):
        latest = _make_result(status="blocked")
        decision, reason = _decide(
            latest=latest,
            stagnation=["hypothesis_loop"],
            state_overrides={"attempts": 4, "max_attempts": 4},
        )
        self.assertEqual(decision, DECISION_STOP)
        self.assertEqual(reason, "stagnation_budget_exhausted")

    def test_max_attempts_without_flag_forces_stop(self):
        latest = _make_result(status="needs_retry")
        decision, reason = _decide(
            latest=latest,
            state_overrides={"attempts": 4, "max_attempts": 4},
        )
        self.assertEqual(decision, DECISION_STOP)
        self.assertEqual(reason, "max_attempts_reached")


class BackendPolicyTests(unittest.TestCase):
    def test_switch_backend_picks_best_by_solve_rate(self):
        memory = {
            "backend_performance": {
                "mock": {"attempts": 3, "solved": 0, "blocked": 3, "avg_confidence": 0.1},
                "claude": {"attempts": 3, "solved": 2, "blocked": 0, "avg_confidence": 0.8},
            }
        }
        index = _select_next_backend_index(
            backend_sequence=["mock", "claude"],
            current_index=0,
            working_memory=memory,
            latest=_make_result(backend="mock"),
        )
        self.assertEqual(index, 1)

    def test_switch_backend_avoids_current_backend(self):
        index = _select_next_backend_index(
            backend_sequence=["a", "b", "c"],
            current_index=0,
            working_memory={},
            latest=_make_result(backend="a"),
        )
        self.assertNotEqual(index, 0)

    def test_empty_sequence_returns_zero(self):
        index = _select_next_backend_index(
            backend_sequence=[],
            current_index=0,
            working_memory={},
            latest=_make_result(),
        )
        self.assertEqual(index, 0)


class StagnationTests(unittest.TestCase):
    def test_three_consecutive_blocked(self):
        history = [
            {"attempt": 1, "status": "blocked", "summary": "a"},
            {"attempt": 2, "status": "blocked", "summary": "b"},
            {"attempt": 3, "status": "blocked", "summary": "c"},
        ]
        signals = _detect_stagnation_signals(history, [], {})
        self.assertIn("three_consecutive_blocked", signals)

    def test_hypothesis_loop(self):
        tested = [
            {"hypothesis": "A", "result": "rejected"},
            {"hypothesis": "A", "result": "rejected"},
            {"hypothesis": "A", "result": "rejected"},
            {"hypothesis": "A", "result": "rejected"},
        ]
        signals = _detect_stagnation_signals([], tested, {})
        self.assertIn("hypothesis_loop", signals)
        self.assertIn("no_confirmed_hypothesis", signals)

    def test_backend_all_blocked(self):
        perf = {"mock": {"attempts": 3, "blocked": 3}}
        signals = _detect_stagnation_signals([], [], perf)
        self.assertIn("backend_all_blocked:mock", signals)


class AttemptBriefTests(unittest.TestCase):
    def test_reframed_brief_warns_against_rejected(self):
        memory = {"rejected_hypotheses": ["xor with key 0x42"], "promising_leads": ["RSA common modulus"]}
        brief = _build_attempt_brief(_make_result(), memory, DECISION_RETRY_REFRAMED)
        self.assertIn("Reframe", brief)
        self.assertIn("xor with key 0x42", brief)

    def test_switch_backend_brief_mentions_strategy(self):
        brief = _build_attempt_brief(_make_result(), {}, DECISION_SWITCH_BACKEND)
        self.assertIn("different backend", brief)

    def test_non_repeating_active_hypothesis_when_reframed(self):
        memory = {"active_hypothesis": "HMAC length extension"}
        brief = _build_attempt_brief(_make_result(hypothesis="HMAC length extension"), memory, DECISION_RETRY_REFRAMED)
        self.assertNotIn("Active hypothesis: HMAC length extension", brief)


class StopSummaryTests(unittest.TestCase):
    def test_stop_summary_contains_core_fields(self):
        state = {"attempts": 4, "max_attempts": 4, "active_backend": "mock"}
        latest = _make_result(status="blocked", hypothesis="sqli error", hypothesis_result="rejected", next_step="pivot to lfi")
        memory = {"stagnation_signals": ["three_consecutive_blocked"], "rejected_hypotheses": ["sqli error"], "promising_leads": ["lfi"]}
        summary = _build_stop_summary(state, latest, "stagnation_signals", memory)
        self.assertIn("Attempts used: 4/4", summary)
        self.assertIn("sqli error", summary)
        self.assertIn("three_consecutive_blocked", summary)
        self.assertIn("lfi", summary)


if __name__ == "__main__":
    unittest.main()
