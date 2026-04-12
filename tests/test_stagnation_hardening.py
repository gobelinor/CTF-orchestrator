import unittest

from ctf_orchestrator.graph import _detect_stagnation_signals, _has_slow_hypothesis_drift, _string_similarity


class SlowDriftTests(unittest.TestCase):
    def test_high_similarity_flagged(self):
        hypotheses = [
            "SQLi on username field",
            "SQLi on username parameter",
            "SQLi on username endpoint",
            "SQLi on username header",
        ]
        self.assertTrue(_has_slow_hypothesis_drift(hypotheses))

    def test_diverse_hypotheses_not_flagged(self):
        hypotheses = [
            "SQLi on login form",
            "Prototype pollution via JSON body",
            "JWT algorithm confusion",
            "SSRF on avatar import",
        ]
        self.assertFalse(_has_slow_hypothesis_drift(hypotheses))


class StringSimilarityTests(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(_string_similarity("foo bar", "foo bar"), 1.0)

    def test_disjoint(self):
        self.assertEqual(_string_similarity("foo", "bar"), 0.0)

    def test_partial(self):
        score = _string_similarity("foo bar baz", "foo bar qux")
        self.assertGreater(score, 0.4)
        self.assertLess(score, 1.0)


class StagnationSignalsTests(unittest.TestCase):
    def test_confidence_downtrend(self):
        tested = [
            {"hypothesis": "A", "result": "inconclusive", "confidence": 0.8},
            {"hypothesis": "B", "result": "inconclusive", "confidence": 0.5},
            {"hypothesis": "C", "result": "inconclusive", "confidence": 0.2},
        ]
        signals = _detect_stagnation_signals([], tested, {})
        self.assertIn("confidence_downtrend", signals)

    def test_no_downtrend_when_stable(self):
        tested = [
            {"hypothesis": "A", "result": "inconclusive", "confidence": 0.5},
            {"hypothesis": "B", "result": "inconclusive", "confidence": 0.55},
            {"hypothesis": "C", "result": "inconclusive", "confidence": 0.5},
        ]
        signals = _detect_stagnation_signals([], tested, {})
        self.assertNotIn("confidence_downtrend", signals)

    def test_command_repetition_detected(self):
        history = [
            {"attempt": 1, "status": "needs_retry", "summary": "s1", "key_commands": ["curl http://t/foo"]},
            {"attempt": 2, "status": "needs_retry", "summary": "s2", "key_commands": ["curl http://t/foo"]},
            {"attempt": 3, "status": "needs_retry", "summary": "s3", "key_commands": ["curl http://t/foo"]},
        ]
        signals = _detect_stagnation_signals(history, [], {})
        self.assertIn("command_repetition", signals)

    def test_slow_drift_signal_integration(self):
        tested = [
            {"hypothesis": "SQLi on /login username field", "result": "rejected", "confidence": 0.2},
            {"hypothesis": "SQLi on /login username param", "result": "rejected", "confidence": 0.2},
            {"hypothesis": "SQLi on /login username cookie", "result": "rejected", "confidence": 0.2},
            {"hypothesis": "SQLi on /login username header", "result": "rejected", "confidence": 0.2},
        ]
        signals = _detect_stagnation_signals([], tested, {})
        self.assertIn("slow_hypothesis_drift", signals)


if __name__ == "__main__":
    unittest.main()
