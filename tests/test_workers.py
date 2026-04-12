import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ctf_orchestrator.skills import Skill
from ctf_orchestrator.workers import (
    ClaudeWorker,
    CodexWorker,
    WorkerRequest,
    _extract_claude_live_command_event,
    _extract_codex_live_command_event,
    _compact_attempts_for_prompt,
    _format_claude_event_line,
    _format_codex_event_line,
)


def _worker_request() -> WorkerRequest:
    return WorkerRequest(
        attempt_index=1,
        challenge_name="Crypto Test",
        challenge_text="Solve the challenge.",
        challenge_category="crypto",
        target_host=None,
        metadata={},
        artifact_paths=[],
        workspace=Path("/tmp"),
        skill=Skill(
            slug="ctf-crypto-solver",
            name="ctf-crypto-solver",
            description="crypto",
            instructions="Follow the crypto workflow.",
            path=Path("/tmp/SKILL.md"),
        ),
        prior_attempts=[],
        working_memory={},
    )


class WorkerTraceTest(unittest.TestCase):
    def test_codex_event_stream_extracts_commands(self) -> None:
        event_stream = "\n".join(
            [
                '{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"/bin/zsh -lc pwd","aggregated_output":"","exit_code":null,"status":"in_progress"}}',
                '{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"/bin/zsh -lc pwd","aggregated_output":"/tmp\\n","exit_code":0,"status":"completed"}}',
                '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"/bin/zsh -lc ls","aggregated_output":"a\\n","exit_code":0,"status":"completed"}}',
            ]
        )
        worker = CodexWorker()
        events = worker._extract_command_events(event_stream)
        commands = worker._extract_commands_from_events(event_stream)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["status"], "completed")
        self.assertEqual(commands, ["/bin/zsh -lc pwd", "/bin/zsh -lc ls"])

    def test_format_codex_event_line_for_console(self) -> None:
        with patch("ctf_orchestrator.workers._current_worker_timestamp", return_value="14:23:01"):
            started = _format_codex_event_line(
                '{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"/bin/zsh -lc pwd","status":"in_progress"}}'
            )
            completed = _format_codex_event_line(
                '{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"/bin/zsh -lc pwd","status":"completed","exit_code":0}}'
            )
        self.assertEqual(started, "[14:23:01] [codex] start: /bin/zsh -lc pwd\n")
        self.assertEqual(completed, "[14:23:01] [codex] done (0): /bin/zsh -lc pwd\n")

    def test_extract_codex_live_command_events(self) -> None:
        started = _extract_codex_live_command_event(
            '{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"/bin/zsh -lc pwd","status":"in_progress"}}'
        )
        completed = _extract_codex_live_command_event(
            '{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"/bin/zsh -lc pwd","status":"completed","exit_code":0}}'
        )
        self.assertEqual(started, ("worker_command_started", {"backend": "codex", "command": "/bin/zsh -lc pwd", "exit_code": None}))
        self.assertEqual(completed, ("worker_command_completed", {"backend": "codex", "command": "/bin/zsh -lc pwd", "exit_code": 0}))

    def test_prompt_attempt_compaction_keeps_key_commands_and_inline_scripts(self) -> None:
        compacted = _compact_attempts_for_prompt(
            [
                {
                    "attempt": 1,
                    "backend": "codex",
                    "status": "needs_retry",
                    "summary": "A" * 400,
                    "next_step": "Inspect generated script and continue.",
                    "evidence": ["fact-1", "fact-2"],
                    "key_commands": ["python3 -c \"print('hello')\"", "ls -la"],
                    "inline_scripts": [{"command": "python3 -c ...", "snippet": "print('hello')"}],
                    "handoff_files": [".runs/working-memory.json"],
                }
            ]
        )
        self.assertEqual(len(compacted), 1)
        self.assertIn("python3 -c", compacted[0]["key_commands"][0])
        self.assertEqual(compacted[0]["inline_scripts"][0]["snippet"], "print('hello')")

    def test_workers_share_provider_agnostic_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            codex = CodexWorker()
            claude = ClaudeWorker()

        self.assertEqual(codex.timeout_seconds, 1800)
        self.assertEqual(claude.timeout_seconds, 1800)
        self.assertEqual(codex.sandbox, "workspace-write")
        self.assertEqual(codex.approval_policy, "never")
        self.assertEqual(claude.permission_mode, "dontAsk")
        self.assertTrue(codex.stream_events)
        self.assertTrue(claude.stream_events)

    def test_workers_honor_common_timeout_and_permission_mode(self) -> None:
        with patch.dict(
            os.environ,
            {"WORKER_TIMEOUT_SECONDS": "42", "WORKER_PERMISSION_MODE": "plan", "WORKER_STREAM_EVENTS": "1"},
            clear=True,
        ):
            codex = CodexWorker()
            claude = ClaudeWorker()

        self.assertEqual(codex.timeout_seconds, 42)
        self.assertEqual(claude.timeout_seconds, 42)
        self.assertEqual((codex.sandbox, codex.approval_policy), ("read-only", "untrusted"))
        self.assertEqual(claude.permission_mode, "plan")
        self.assertTrue(codex.stream_events)
        self.assertTrue(claude.stream_events)

    def test_claude_command_does_not_duplicate_skill_instructions(self) -> None:
        request = _worker_request()
        with patch.dict(os.environ, {}, clear=True):
            worker = ClaudeWorker()

        command = worker._build_command(
            request,
            Path("/tmp/schema.json"),
            Path("/tmp/output.json"),
            "PROMPT BODY",
        )

        self.assertNotIn("--append-system-prompt", command)
        self.assertEqual(command[-1], "PROMPT BODY")
        self.assertIn("--verbose", command)
        self.assertIn("stream-json", command)
        self.assertIn("--no-session-persistence", command)

    def test_claude_event_stream_extracts_commands(self) -> None:
        event_stream = "\n".join(
            [
                '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_1","name":"Bash","input":{"command":"pwd"}}]}}',
                '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"ok","is_error":false}]}}',
            ]
        )
        worker = ClaudeWorker()
        events = worker._extract_command_events(event_stream)
        commands = worker._extract_commands_from_events(event_stream)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["command"], "pwd")
        self.assertEqual(events[0]["status"], "completed")
        self.assertEqual(commands, ["pwd"])

    def test_format_claude_event_line_for_console(self) -> None:
        with patch("ctf_orchestrator.workers._current_worker_timestamp", return_value="14:23:01"):
            started = _format_claude_event_line(
                '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_1","name":"Bash","input":{"command":"pwd"}}]}}'
            )
            completed = _format_claude_event_line(
                '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"ok","is_error":false}]}}'
            )
        self.assertEqual(started, "[14:23:01] [claude] start: /bin/zsh -lc pwd\n")
        self.assertEqual(completed, "[14:23:01] [claude] done (ok): toolu_1\n")

    def test_extract_claude_live_command_events(self) -> None:
        live_state: dict[str, str] = {}
        started = _extract_claude_live_command_event(
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_1","name":"Bash","input":{"command":"pwd"}}]}}',
            live_state,
        )
        completed = _extract_claude_live_command_event(
            '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"ok","is_error":false}]}}',
            live_state,
        )
        self.assertEqual(
            started,
            ("worker_command_started", {"backend": "claude", "command": "/bin/zsh -lc pwd", "exit_code": None}),
        )
        self.assertEqual(
            completed,
            ("worker_command_completed", {"backend": "claude", "command": "/bin/zsh -lc pwd", "exit_code": 0}),
        )

    def test_codex_streaming_timeout_terminates_process_tree(self) -> None:
        class FakeTimedOutProcess:
            stdin = None
            stdout = None
            stderr = None
            pid = 4242

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)

        with TemporaryDirectory() as tmp_dir:
            request = _worker_request()
            request = WorkerRequest(
                attempt_index=request.attempt_index,
                challenge_name=request.challenge_name,
                challenge_text=request.challenge_text,
                challenge_category=request.challenge_category,
                target_host=request.target_host,
                metadata=request.metadata,
                artifact_paths=request.artifact_paths,
                workspace=Path(tmp_dir),
                skill=request.skill,
                prior_attempts=request.prior_attempts,
                working_memory=request.working_memory,
            )
            with patch.dict(os.environ, {"WORKER_STREAM_EVENTS": "1"}, clear=True):
                worker = CodexWorker()
            worker.timeout_seconds = 1
            with (
                patch.object(CodexWorker, "_build_command", return_value=["codex"]),
                patch("ctf_orchestrator.workers.subprocess.Popen", return_value=FakeTimedOutProcess()),
                patch("ctf_orchestrator.workers._terminate_process_tree") as terminate_process_tree,
            ):
                result = worker.invoke(request)

        self.assertEqual(result.status, "blocked")
        self.assertIn("timed out after 1 seconds", result.summary)
        terminate_process_tree.assert_called_once()


if __name__ == "__main__":
    unittest.main()
