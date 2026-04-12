import unittest
from dataclasses import dataclass
from unittest.mock import patch

from ctf_orchestrator import supervisor as supervisor_module
from ctf_orchestrator.campaign.models import CampaignCapacities, CampaignFilters
from ctf_orchestrator.supervisor_agent import SupervisorDecision


@dataclass
class _FakeRecord:
    challenge_name: str
    challenge_key: str
    category: str
    priority_reason: str
    instance_required: bool
    previous_failures: int
    status: str
    priority_score: float = 0.0
    last_summary: str = ""


class _FakeCampaignState:
    campaign_name = "demo"
    source_label = "https://ctf.example/challenges"
    capacities = CampaignCapacities(max_parallel_challenges=2, max_instance_challenges=1)
    filters = CampaignFilters()

    def counts_by_status(self):
        return {"pending": 1, "solved": 1}


class _FakeRequest:
    def __init__(self):
        self.skills_root = object()
        self.backend_sequence = ["claude"]


class SupervisorAgentApplicationTests(unittest.TestCase):
    def test_needs_human_decision_marks_record(self):
        record = _FakeRecord("A", "k-a", "web", "priority", False, 0, "needs_human")
        decision = SupervisorDecision(
            decision="needs_human",
            reason="worker flagged credentials required",
            next_backend=None,
            next_brief="",
            promote_priority=False,
            demote_priority=False,
            notes="credentials",
        )
        events: list[tuple[str, dict]] = []

        with patch.object(supervisor_module, "decide_post_challenge", return_value=decision):
            supervisor_module._apply_supervisor_agent_decision(
                state=_FakeCampaignState(),
                record=record,
                final_state={"solved": False},
                request=_FakeRequest(),
                emit=lambda t, p: events.append((t, p)),
            )
        self.assertEqual(record.status, "needs_human")
        self.assertTrue(any(t == "supervisor_agent_decision" for t, _ in events))

    def test_retry_reframed_decision_resets_to_pending_and_stores_brief(self):
        record = _FakeRecord("A", "k-a", "web", "priority", False, 1, "needs_human")
        decision = SupervisorDecision(
            decision="retry_same_backend_reframed",
            reason="stagnation",
            next_backend="claude",
            next_brief="Avoid SSRF; try auth bypass on /admin",
            promote_priority=True,
            demote_priority=False,
            notes="",
        )

        with patch.object(supervisor_module, "decide_post_challenge", return_value=decision):
            supervisor_module._apply_supervisor_agent_decision(
                state=_FakeCampaignState(),
                record=record,
                final_state={"solved": False},
                request=_FakeRequest(),
                emit=lambda t, p: None,
            )
        self.assertEqual(record.status, "pending")
        self.assertIn("auth bypass", record.last_summary)
        self.assertGreater(record.priority_score, 0)

    def test_skip_decision_marks_skipped(self):
        record = _FakeRecord("A", "k-a", "web", "priority", False, 0, "needs_human")
        decision = SupervisorDecision(
            decision="skip",
            reason="out of scope",
            next_backend=None,
            next_brief="",
            promote_priority=False,
            demote_priority=True,
            notes="",
        )
        with patch.object(supervisor_module, "decide_post_challenge", return_value=decision):
            supervisor_module._apply_supervisor_agent_decision(
                state=_FakeCampaignState(),
                record=record,
                final_state={"solved": False},
                request=_FakeRequest(),
                emit=lambda t, p: None,
            )
        self.assertEqual(record.status, "skipped")
        self.assertLess(record.priority_score, 0)

    def test_agent_unavailable_preserves_status(self):
        record = _FakeRecord("A", "k-a", "web", "priority", False, 0, "needs_human")
        with patch.object(supervisor_module, "decide_post_challenge", return_value=None):
            supervisor_module._apply_supervisor_agent_decision(
                state=_FakeCampaignState(),
                record=record,
                final_state={"solved": False},
                request=_FakeRequest(),
                emit=lambda t, p: None,
            )
        self.assertEqual(record.status, "needs_human")

    def test_agent_exception_is_caught_and_emits_error(self):
        record = _FakeRecord("A", "k-a", "web", "priority", False, 0, "needs_human")
        events: list[tuple[str, dict]] = []
        with patch.object(
            supervisor_module,
            "decide_post_challenge",
            side_effect=RuntimeError("boom"),
        ):
            supervisor_module._apply_supervisor_agent_decision(
                state=_FakeCampaignState(),
                record=record,
                final_state={"solved": False},
                request=_FakeRequest(),
                emit=lambda t, p: events.append((t, p)),
            )
        self.assertTrue(any(t == "supervisor_agent_error" for t, _ in events))


if __name__ == "__main__":
    unittest.main()
