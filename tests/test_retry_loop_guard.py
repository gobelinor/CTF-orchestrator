import unittest

from ctf_orchestrator.graph import (
    DECISION_RETRY_REFRAMED,
    DECISION_RETRY_SAME,
    _choose_decision,
)
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


class RetryLoopGuardTests(unittest.TestCase):
    def test_identical_hypothesis_triggers_reframed(self):
        history = [
            {"attempt": 1, "backend": "mock", "status": "needs_retry", "hypothesis": "inject X"},
            {"attempt": 2, "backend": "mock", "status": "needs_retry", "hypothesis": "inject X"},
        ]
        latest = _result(status="needs_retry", hypothesis="inject X", confidence=0.2)
        # status=="needs_retry" returns DECISION_RETRY_REFRAMED early anyway, so
        # use a status that falls through to the identical-repeat guard.
        latest = _result(status="solved", hypothesis="inject X", confidence=0.2, flag=None)
        decision, reason = _choose_decision(
            state={"backend_sequence": ["mock"], "attempts": 2},
            latest=latest,
            working_memory={},
            history=history,
            attempts=2,
            max_attempts=4,
            stagnation=[],
        )
        self.assertEqual(decision, DECISION_RETRY_REFRAMED)
        self.assertEqual(reason, "identical_repeat_guard")

    def test_no_hypothesis_still_has_bounded_path(self):
        """Default path must never be DECISION_RETRY_SAME without any signal."""
        latest = _result(status="solved", hypothesis="", confidence=0.1)
        decision, reason = _choose_decision(
            state={"backend_sequence": ["mock"], "attempts": 1},
            latest=latest,
            working_memory={},
            history=[],
            attempts=1,
            max_attempts=4,
            stagnation=[],
        )
        self.assertNotEqual(decision, DECISION_RETRY_SAME)


if __name__ == "__main__":
    unittest.main()
