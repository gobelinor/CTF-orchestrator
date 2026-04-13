from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import textwrap
import threading
from typing import Any

from .skills import Skill


RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["solved", "needs_retry", "blocked"],
        },
        "summary": {"type": "string"},
        "next_step": {"type": "string"},
        "flag": {"type": ["string", "null"]},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "commands": {"type": "array", "items": {"type": "string"}},
        "hypothesis": {"type": "string"},
        "hypothesis_result": {
            "type": "string",
            "enum": ["confirmed", "rejected", "inconclusive", "untested"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "novelty": {"type": "number", "minimum": 0, "maximum": 1},
        "failure_reason": {
            "type": "string",
            "enum": [
                "none",
                "target_unreachable",
                "auth_required",
                "wrong_category",
                "wrong_hypothesis",
                "tool_missing",
                "time_budget",
                "needs_human",
                "worker_error",
                "unknown",
            ],
        },
        "recommended_action": {
            "type": "string",
            "enum": [
                "retry_same_backend",
                "retry_same_backend_reframed",
                "switch_backend",
                "stop",
                "request_writeup",
                "request_memory_persist",
                "reassess_category",
                "needs_human",
            ],
        },
        "artifacts_produced": {"type": "array", "items": {"type": "string"}},
        "network_touched": {"type": "boolean"},
        "target_reachable": {"type": "boolean"},
        "needs_human": {"type": "boolean"},
        "branch_id": {"type": "string"},
    },
    "required": [
        "status",
        "summary",
        "next_step",
        "flag",
        "evidence",
        "commands",
        "hypothesis",
        "hypothesis_result",
        "confidence",
        "failure_reason",
        "recommended_action",
    ],
    "additionalProperties": False,
}

# Permissive flag scanner used only as a fallback when the agent did not fill
# `flag` directly. Catches any short word-like prefix (2-24 chars: letters,
# digits, underscore, hyphen) followed by `{...}`. Covers HTB{}, THM{},
# picoCTF{}, flag{}, CTF{}, EPT{}, ABCD{}, n00b{}, UTCTF{}, and most real
# platform conventions. The agent's own `flag` field remains the source of
# truth via WorkerResult; this regex only rescues flags that leaked into
# summary/raw_output.
FLAG_RE = re.compile(r"[A-Za-z0-9_\-]{2,24}\{[^}\n\r]{1,512}\}")


@dataclass(frozen=True)
class WorkerRequest:
    attempt_index: int
    challenge_name: str
    challenge_text: str
    challenge_category: str | None
    target_host: str | None
    metadata: dict[str, Any]
    artifact_paths: list[str]
    workspace: Path
    skill: Skill
    prior_attempts: list[dict[str, Any]]
    working_memory: dict[str, Any]
    core_skill: Skill | None = None


@dataclass(frozen=True)
class WorkerResult:
    backend: str
    status: str
    summary: str
    next_step: str
    flag: str | None = None
    evidence: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    command_events: list[dict[str, Any]] = field(default_factory=list)
    event_log_path: str = ""
    raw_output: str = ""
    hypothesis: str = ""
    hypothesis_result: str = "untested"
    confidence: float = 0.0
    novelty: float = 0.0
    failure_reason: str = "none"
    recommended_action: str = ""
    artifacts_produced: list[str] = field(default_factory=list)
    network_touched: bool = False
    target_reachable: bool = False
    needs_human: bool = False
    branch_id: str = ""

    def as_state_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "WorkerResult":
        return cls(
            backend=payload["backend"],
            status=payload["status"],
            summary=payload.get("summary", ""),
            next_step=payload.get("next_step", ""),
            flag=payload.get("flag"),
            evidence=list(payload.get("evidence", [])),
            commands=list(payload.get("commands", [])),
            command_events=list(payload.get("command_events", [])),
            event_log_path=payload.get("event_log_path", ""),
            raw_output=payload.get("raw_output", ""),
            hypothesis=payload.get("hypothesis", ""),
            hypothesis_result=payload.get("hypothesis_result", "untested"),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            novelty=float(payload.get("novelty", 0.0) or 0.0),
            failure_reason=payload.get("failure_reason", "none"),
            recommended_action=payload.get("recommended_action", ""),
            artifacts_produced=list(payload.get("artifacts_produced", [])),
            network_touched=bool(payload.get("network_touched", False)),
            target_reachable=bool(payload.get("target_reachable", False)),
            needs_human=bool(payload.get("needs_human", False)),
            branch_id=payload.get("branch_id", ""),
        )


def _extract_extended_fields(parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "hypothesis": str(parsed.get("hypothesis", "") or ""),
        "hypothesis_result": str(parsed.get("hypothesis_result", "untested") or "untested"),
        "confidence": float(parsed.get("confidence", 0.0) or 0.0),
        "novelty": float(parsed.get("novelty", 0.0) or 0.0),
        "failure_reason": str(parsed.get("failure_reason", "none") or "none"),
        "recommended_action": str(parsed.get("recommended_action", "") or ""),
        "artifacts_produced": list(parsed.get("artifacts_produced", []) or []),
        "network_touched": bool(parsed.get("network_touched", False)),
        "target_reachable": bool(parsed.get("target_reachable", False)),
        "needs_human": bool(parsed.get("needs_human", False)),
        "branch_id": str(parsed.get("branch_id", "") or ""),
    }


class WorkerBackend(ABC):
    name: str

    @abstractmethod
    def invoke(
        self,
        request: WorkerRequest,
        event_sink: Any = None,
    ) -> WorkerResult:
        raise NotImplementedError


class MockWorker(WorkerBackend):
    name = "mock"

    def invoke(
        self,
        request: WorkerRequest,
        event_sink: Any = None,
    ) -> WorkerResult:
        flag_match = FLAG_RE.search(request.challenge_text)
        if flag_match:
            return WorkerResult(
                backend=self.name,
                status="solved",
                summary="Flag already present in challenge text.",
                next_step="Submit the flag to validate the challenge.",
                flag=flag_match.group(0),
                evidence=["Detected an inline flag pattern in the challenge statement."],
                hypothesis="Flag inlined in challenge statement.",
                hypothesis_result="confirmed",
                confidence=1.0,
                novelty=0.1,
                failure_reason="none",
                recommended_action="request_writeup",
                target_reachable=True,
            )

        if request.attempt_index == 1:
            artifact_note = (
                f"Staged artifacts available in workspace: {', '.join(request.artifact_paths)}"
                if request.artifact_paths
                else "No artifacts were provided, so only the challenge text was analyzed."
            )
            return WorkerResult(
                backend=self.name,
                status="needs_retry",
                summary=f"First pass using {request.skill.slug}: likely auth bypass or input tampering path.",
                next_step="Retry with another worker or increase tool autonomy to validate the main hypothesis.",
                evidence=[
                    f"Skill selected: {request.skill.slug}",
                    artifact_note,
                ],
                commands=["curl -i http://target/login", "ffuf -w wordlist.txt -u http://target/FUZZ"],
                hypothesis="Auth bypass or input tampering path worth probing.",
                hypothesis_result="inconclusive",
                confidence=0.4,
                novelty=0.6,
                failure_reason="none",
                recommended_action="switch_backend",
            )

        return WorkerResult(
            backend=self.name,
            status="blocked",
            summary="Mock worker exhausted its deterministic branches without recovering a flag.",
            next_step="Escalate to a real backend such as codex or claude.",
            evidence=["This is the expected fallback branch of the mock backend."],
            hypothesis="",
            hypothesis_result="untested",
            confidence=0.0,
            novelty=0.0,
            failure_reason="worker_error",
            recommended_action="switch_backend",
        )


class SubprocessWorker(WorkerBackend, ABC):
    def __init__(self, name: str, timeout_seconds: int = 600) -> None:
        self.name = name
        self.timeout_seconds = timeout_seconds

    def invoke(
        self,
        request: WorkerRequest,
        event_sink: Any = None,
    ) -> WorkerResult:
        run_dir = request.workspace / ".runs" / self.name
        run_dir.mkdir(parents=True, exist_ok=True)
        schema_path = run_dir / f"attempt-{request.attempt_index:02d}-schema.json"
        output_path = run_dir / f"attempt-{request.attempt_index:02d}-output.json"
        prompt_path = run_dir / f"attempt-{request.attempt_index:02d}-prompt.txt"
        events_path = run_dir / f"attempt-{request.attempt_index:02d}-events.jsonl"
        schema_path.write_text(json.dumps(RESULT_SCHEMA, indent=2), encoding="utf-8")
        prompt = self._build_prompt(request)
        prompt_path.write_text(prompt, encoding="utf-8")

        try:
            completed = subprocess.run(
                self._build_command(request, schema_path, output_path, prompt),
                cwd=request.workspace,
                input=self._stdin_payload(prompt),
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_text = _to_text(exc.stdout)
            if stdout_text:
                events_path.write_text(stdout_text, encoding="utf-8")
            stderr_text = _to_text(exc.stderr)
            raw_output = self._read_output_file(output_path) or stdout_text or stderr_text
            parsed = self._parse_if_possible(raw_output)
            command_events = self._extract_command_events(stdout_text)
            if parsed is not None:
                commands = _dedupe_strings(
                    list(parsed.get("commands", [])) + self._extract_commands_from_events(stdout_text)
                )
                return WorkerResult(
                    backend=self.name,
                    status=parsed["status"],
                    summary=parsed["summary"],
                    next_step=parsed["next_step"],
                    flag=parsed.get("flag"),
                    evidence=list(parsed.get("evidence", [])),
                    commands=commands,
                    command_events=command_events,
                    event_log_path=str(events_path) if stdout_text else "",
                    raw_output=raw_output,
                    **_extract_extended_fields(parsed),
                )
            return WorkerResult(
                backend=self.name,
                status="blocked",
                summary=f"{self.name} timed out after {self.timeout_seconds} seconds.",
                next_step="Retry with a longer timeout or a narrower prompt.",
                evidence=[(stderr_text or stdout_text or "No subprocess output before timeout.").strip()],
                commands=_dedupe_strings(self._extract_commands_from_events(stdout_text)),
                command_events=command_events,
                event_log_path=str(events_path) if stdout_text else "",
                raw_output=raw_output,
                hypothesis="",
                hypothesis_result="untested",
                confidence=0.0,
                failure_reason="time_budget",
                recommended_action="switch_backend",
            )
        if completed.stdout:
            events_path.write_text(completed.stdout, encoding="utf-8")
        raw_output = self._read_output_file(output_path) or completed.stdout or completed.stderr
        command_events = self._extract_command_events(completed.stdout or "")
        parsed = self._parse_if_possible(raw_output)
        if completed.returncode != 0 and parsed is None:
            return WorkerResult(
                backend=self.name,
                status="blocked",
                summary=f"{self.name} exited with status {completed.returncode}.",
                next_step="Inspect stderr/stdout and adjust authentication or CLI flags.",
                evidence=[completed.stderr.strip() or completed.stdout.strip() or "No subprocess output."],
                commands=_dedupe_strings(self._extract_commands_from_events(completed.stdout or "")),
                command_events=command_events,
                event_log_path=str(events_path) if completed.stdout else "",
                raw_output=raw_output,
                hypothesis="",
                hypothesis_result="untested",
                confidence=0.0,
                failure_reason="worker_error",
                recommended_action="switch_backend",
            )

        if parsed is None:
            parsed = self._parse_structured_output(raw_output)
        commands = _dedupe_strings(
            list(parsed.get("commands", [])) + self._extract_commands_from_events(completed.stdout or "")
        )
        return WorkerResult(
            backend=self.name,
            status=parsed["status"],
            summary=parsed["summary"],
            next_step=parsed["next_step"],
            flag=parsed.get("flag"),
            evidence=list(parsed.get("evidence", [])),
            commands=commands,
            command_events=command_events,
            event_log_path=str(events_path) if completed.stdout else "",
            raw_output=raw_output,
            **_extract_extended_fields(parsed),
        )

    def _build_prompt(self, request: WorkerRequest) -> str:
        prior_attempts = (
            json.dumps(_compact_attempts_for_prompt(request.prior_attempts), indent=2, ensure_ascii=False)
            if request.prior_attempts
            else "[]"
        )
        working_memory = (
            json.dumps(request.working_memory, indent=2, ensure_ascii=False)
            if request.working_memory
            else "{}"
        )
        artifacts = "\n".join(f"- {path}" for path in request.artifact_paths) or "- none"
        metadata = json.dumps(request.metadata, indent=2, ensure_ascii=False) if request.metadata else "{}"
        core_skill_section = ""
        if request.core_skill is not None:
            core_skill_section = textwrap.dedent(
                f"""
                Core methodology skill: {request.core_skill.name}
                Core methodology description: {request.core_skill.description}

                Core methodology instructions:
                {request.core_skill.instructions}

                """
            )
        return textwrap.dedent(
            f"""
            Challenge name: {request.challenge_name}
            Attempt: {request.attempt_index}
            Workspace: {request.workspace}
            Declared category: {request.challenge_category or "unknown"}
            Target host: {request.target_host or "none"}

            Challenge text:
            {request.challenge_text}

            Artifact paths:
            {artifacts}

            Challenge metadata:
            {metadata}

            Prior attempts:
            {prior_attempts}

            Structured handoff memory:
            {working_memory}

            {core_skill_section}Specialist skill: {request.skill.name}
            Specialist skill description: {request.skill.description}

            Specialist skill instructions:
            {request.skill.instructions}

            Objective:
            - Solve the CTF challenge if possible.
            - Apply the core methodology first, then the specialist workflow.
            - You may execute shell commands inside the workspace when needed.
            - If a target host is provided, prefer direct interaction with it using shell tools before relying on external references.
            - Treat the workspace as persistent across attempts. Reuse existing scripts, logs and notes before restarting naive reconnaissance.
            - For attempts greater than 1, inspect the handoff files and key commands in the structured memory before launching new exploration.
            - Avoid repeating commands or hypotheses already marked low-value unless you have a concrete reason.
            - If you cannot finish, return the most useful next step.
            - If you find a candidate flag, include it in the `flag` field only.
            - Keep evidence concise and operational.
            - Include the key commands you executed in the `commands` field, especially any long inline scripts or one-liners that would otherwise be lost.
            - Do not rely on public writeups alone when direct interaction is feasible from the workspace.

            Required structured fields (in addition to status/summary/next_step/flag/evidence/commands):
            - `hypothesis`: one sentence describing the exact attack idea you pursued this attempt.
            - `hypothesis_result`: one of confirmed | rejected | inconclusive | untested.
            - `confidence`: 0.0..1.0, how sure you are the hypothesis matches reality.
            - `novelty`: 0.0..1.0, how different this hypothesis is from previous tested hypotheses in working memory. Penalize repetition.
            - `failure_reason`: one of none | target_unreachable | auth_required | wrong_category | wrong_hypothesis | tool_missing | time_budget | needs_human | worker_error | unknown. Use "none" on success.
            - `recommended_action`: one of retry_same_backend | retry_same_backend_reframed | switch_backend | stop | request_writeup | request_memory_persist | reassess_category | needs_human. Pick what should happen next from the supervisor's point of view.
            - `artifacts_produced`: list of workspace-relative file paths that you produced this attempt and that are worth keeping.
            - `network_touched`: true if you performed any outbound network interaction.
            - `target_reachable`: true if you confirmed the target host actually responds.
            - `needs_human`: true only if progress truly requires external human input (credentials, approvals, hints).
            - `branch_id`: short identifier for the exploration branch you are on (e.g. "sqli-error-based", "rsa-common-modulus"). Reuse the same id across attempts that continue the same line of investigation.

            A Graphiti knowledge graph is available via MCP in this environment. When useful (known techniques, prior writeups, target-specific notes) you may search it before issuing new commands. Do NOT store anything into Graphiti yourself here — a dedicated memory agent handles persistence after the challenge is resolved.
            """
        ).strip()

    def _parse_structured_output(self, raw_output: str) -> dict[str, Any]:
        payload = _extract_json(raw_output)
        if not isinstance(payload, dict):
            raise ValueError(f"Expected a JSON object from {self.name}, got: {type(payload).__name__}")
        if "status" in payload:
            return payload

        for nested_key in ("result", "content", "message", "output"):
            nested = payload.get(nested_key)
            if isinstance(nested, str):
                nested_payload = _extract_json(nested)
                if isinstance(nested_payload, dict) and "status" in nested_payload:
                    return nested_payload

        raise ValueError(f"Unable to locate a structured worker result in {self.name} output.")

    def _parse_if_possible(self, raw_output: str) -> dict[str, Any] | None:
        try:
            return self._parse_structured_output(raw_output)
        except Exception:
            return None

    def _extract_command_events(self, event_stream: str) -> list[dict[str, Any]]:
        return []

    def _extract_commands_from_events(self, event_stream: str) -> list[str]:
        return [event["command"] for event in self._extract_command_events(event_stream) if event.get("command")]

    def _read_output_file(self, output_path: Path) -> str:
        if output_path.exists():
            return output_path.read_text(encoding="utf-8").strip()
        return ""

    def _stdin_payload(self, prompt: str) -> str | None:
        return None

    @abstractmethod
    def _build_command(
        self,
        request: WorkerRequest,
        schema_path: Path,
        output_path: Path,
        prompt: str,
    ) -> list[str]:
        raise NotImplementedError


class CodexWorker(SubprocessWorker):
    def __init__(self) -> None:
        super().__init__(name="codex", timeout_seconds=_resolve_worker_timeout_seconds())
        self.model = os.getenv("CODEX_MODEL", "")
        self.sandbox, self.approval_policy = _resolve_codex_execution_policy()
        self.extra_args: list[str] = []
        self.stream_events = True

    def invoke(
        self,
        request: WorkerRequest,
        event_sink: Any = None,
    ) -> WorkerResult:
        if not self.stream_events:
            return super().invoke(request, event_sink=event_sink)

        run_dir = request.workspace / ".runs" / self.name
        run_dir.mkdir(parents=True, exist_ok=True)
        schema_path = run_dir / f"attempt-{request.attempt_index:02d}-schema.json"
        output_path = run_dir / f"attempt-{request.attempt_index:02d}-output.json"
        prompt_path = run_dir / f"attempt-{request.attempt_index:02d}-prompt.txt"
        events_path = run_dir / f"attempt-{request.attempt_index:02d}-events.jsonl"
        schema_path.write_text(json.dumps(RESULT_SCHEMA, indent=2), encoding="utf-8")
        prompt = self._build_prompt(request)
        prompt_path.write_text(prompt, encoding="utf-8")

        command = self._build_command(request, schema_path, output_path, prompt)
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        process = subprocess.Popen(
            command,
            cwd=request.workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            **_streaming_popen_kwargs(),
        )
        stdin_payload = self._stdin_payload(prompt)
        if stdin_payload is not None and process.stdin is not None:
            process.stdin.write(stdin_payload)
            process.stdin.close()

        stdout_thread = threading.Thread(
            target=_read_stream_lines,
            args=(process.stdout, stdout_chunks, lambda line: self._emit_live_event(line, event_sink)),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_read_stream_lines,
            args=(process.stderr, stderr_chunks, None),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        try:
            returncode = process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(process)
            returncode = None

        stream_join_warnings = _join_stream_threads(stdout_thread, stderr_thread)
        stdout_text = "".join(stdout_chunks)
        stderr_text = "".join(stderr_chunks)
        if stream_join_warnings:
            stderr_text = "\n".join(stream_join_warnings + ([stderr_text] if stderr_text else []))
        if stdout_text:
            events_path.write_text(stdout_text, encoding="utf-8")

        raw_output = self._read_output_file(output_path) or stdout_text or stderr_text
        command_events = self._extract_command_events(stdout_text)
        parsed = self._parse_if_possible(raw_output)
        if timed_out:
            if parsed is not None:
                commands = _dedupe_strings(
                    list(parsed.get("commands", [])) + self._extract_commands_from_events(stdout_text)
                )
                return WorkerResult(
                    backend=self.name,
                    status=parsed["status"],
                    summary=parsed["summary"],
                    next_step=parsed["next_step"],
                    flag=parsed.get("flag"),
                    evidence=list(parsed.get("evidence", [])),
                    commands=commands,
                    command_events=command_events,
                    event_log_path=str(events_path) if stdout_text else "",
                    raw_output=raw_output,
                    **_extract_extended_fields(parsed),
                )
            return WorkerResult(
                backend=self.name,
                status="blocked",
                summary=f"{self.name} timed out after {self.timeout_seconds} seconds.",
                next_step="Retry with a longer timeout or a narrower prompt.",
                evidence=[(stderr_text or stdout_text or "No subprocess output before timeout.").strip()],
                commands=_dedupe_strings(self._extract_commands_from_events(stdout_text)),
                command_events=command_events,
                event_log_path=str(events_path) if stdout_text else "",
                raw_output=raw_output,
                hypothesis="",
                hypothesis_result="untested",
                confidence=0.0,
                failure_reason="time_budget",
                recommended_action="switch_backend",
            )

        if returncode not in (0, None) and parsed is None:
            return WorkerResult(
                backend=self.name,
                status="blocked",
                summary=f"{self.name} exited with status {returncode}.",
                next_step="Inspect stderr/stdout and adjust authentication or CLI flags.",
                evidence=[(stderr_text or stdout_text or "No subprocess output.").strip()],
                commands=_dedupe_strings(self._extract_commands_from_events(stdout_text)),
                command_events=command_events,
                event_log_path=str(events_path) if stdout_text else "",
                raw_output=raw_output,
                hypothesis="",
                hypothesis_result="untested",
                confidence=0.0,
                failure_reason="worker_error",
                recommended_action="switch_backend",
            )

        if parsed is None:
            parsed = self._parse_structured_output(raw_output)
        commands = _dedupe_strings(
            list(parsed.get("commands", [])) + self._extract_commands_from_events(stdout_text)
        )
        return WorkerResult(
            backend=self.name,
            status=parsed["status"],
            summary=parsed["summary"],
            next_step=parsed["next_step"],
            flag=parsed.get("flag"),
            evidence=list(parsed.get("evidence", [])),
            commands=commands,
            command_events=command_events,
            event_log_path=str(events_path) if stdout_text else "",
            raw_output=raw_output,
            **_extract_extended_fields(parsed),
        )

    def _stdin_payload(self, prompt: str) -> str | None:
        return prompt

    def _build_command(
        self,
        request: WorkerRequest,
        schema_path: Path,
        output_path: Path,
        prompt: str,
    ) -> list[str]:
        command = [
            "codex",
            "-a",
            self.approval_policy,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            self.sandbox,
            "--json",
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
            "-C",
            str(request.workspace),
            "-",
        ]
        if self.model:
            command[3:3] = ["-m", self.model]
        return command[:-1] + self.extra_args + command[-1:]

    def _extract_command_events(self, event_stream: str) -> list[dict[str, Any]]:
        latest_by_id: dict[str, dict[str, Any]] = {}
        ordered_ids: list[str] = []
        for line in event_stream.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = payload.get("item")
            if not isinstance(item, dict) or item.get("type") != "command_execution":
                continue
            item_id = str(item.get("id", len(ordered_ids)))
            latest_by_id[item_id] = {
                "id": item_id,
                "event_type": payload.get("type", ""),
                "command": item.get("command", ""),
                "status": item.get("status", ""),
                "exit_code": item.get("exit_code"),
                "output": item.get("aggregated_output", ""),
            }
            if item_id not in ordered_ids:
                ordered_ids.append(item_id)
        return [latest_by_id[item_id] for item_id in ordered_ids]

    def _emit_live_event(self, line: str, event_sink: Any = None) -> None:
        formatted = _format_codex_event_line(line)
        if formatted:
            sys.stderr.write(formatted)
            sys.stderr.flush()
        event = _extract_codex_live_command_event(line)
        if event and event_sink is not None:
            event_type, payload = event
            event_sink(event_type, payload)


class ClaudeWorker(SubprocessWorker):
    def __init__(self) -> None:
        super().__init__(name="claude", timeout_seconds=_resolve_worker_timeout_seconds())
        self.model = os.getenv("CLAUDE_MODEL", "")
        self.permission_mode = _resolve_claude_permission_mode()
        self.extra_args: list[str] = []
        self.stream_events = True

    def invoke(
        self,
        request: WorkerRequest,
        event_sink: Any = None,
    ) -> WorkerResult:
        if not self.stream_events:
            return super().invoke(request, event_sink=event_sink)

        run_dir = request.workspace / ".runs" / self.name
        run_dir.mkdir(parents=True, exist_ok=True)
        schema_path = run_dir / f"attempt-{request.attempt_index:02d}-schema.json"
        output_path = run_dir / f"attempt-{request.attempt_index:02d}-output.json"
        prompt_path = run_dir / f"attempt-{request.attempt_index:02d}-prompt.txt"
        events_path = run_dir / f"attempt-{request.attempt_index:02d}-events.jsonl"
        schema_path.write_text(json.dumps(RESULT_SCHEMA, indent=2), encoding="utf-8")
        prompt = self._build_prompt(request)
        prompt_path.write_text(prompt, encoding="utf-8")

        command = self._build_command(request, schema_path, output_path, prompt)
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        process = subprocess.Popen(
            command,
            cwd=request.workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            **_streaming_popen_kwargs(),
        )

        live_state: dict[str, str] = {}
        stdout_thread = threading.Thread(
            target=_read_stream_lines,
            args=(process.stdout, stdout_chunks, lambda line: self._emit_live_event(line, event_sink, live_state)),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_read_stream_lines,
            args=(process.stderr, stderr_chunks, None),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        try:
            returncode = process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(process)
            returncode = None

        stream_join_warnings = _join_stream_threads(stdout_thread, stderr_thread)
        stdout_text = "".join(stdout_chunks)
        stderr_text = "".join(stderr_chunks)
        if stream_join_warnings:
            stderr_text = "\n".join(stream_join_warnings + ([stderr_text] if stderr_text else []))
        if stdout_text:
            events_path.write_text(stdout_text, encoding="utf-8")

        stream_payload = self._extract_claude_stream_payload(stdout_text)
        if stream_payload is not None:
            output_path.write_text(json.dumps(stream_payload, indent=2, ensure_ascii=False), encoding="utf-8")

        raw_output = (
            json.dumps(stream_payload, ensure_ascii=False)
            if stream_payload is not None
            else self._read_output_file(output_path) or stdout_text or stderr_text
        )
        command_events = self._extract_command_events(stdout_text)
        parsed = stream_payload or self._parse_if_possible(raw_output)

        if timed_out:
            if parsed is not None:
                commands = _dedupe_strings(
                    list(parsed.get("commands", [])) + self._extract_commands_from_events(stdout_text)
                )
                return WorkerResult(
                    backend=self.name,
                    status=parsed["status"],
                    summary=parsed["summary"],
                    next_step=parsed["next_step"],
                    flag=parsed.get("flag"),
                    evidence=list(parsed.get("evidence", [])),
                    commands=commands,
                    command_events=command_events,
                    event_log_path=str(events_path) if stdout_text else "",
                    raw_output=raw_output,
                    **_extract_extended_fields(parsed),
                )
            return WorkerResult(
                backend=self.name,
                status="blocked",
                summary=f"{self.name} timed out after {self.timeout_seconds} seconds.",
                next_step="Retry with a longer timeout or a narrower prompt.",
                evidence=[(stderr_text or stdout_text or "No subprocess output before timeout.").strip()],
                commands=_dedupe_strings(self._extract_commands_from_events(stdout_text)),
                command_events=command_events,
                event_log_path=str(events_path) if stdout_text else "",
                raw_output=raw_output,
                hypothesis="",
                hypothesis_result="untested",
                confidence=0.0,
                failure_reason="time_budget",
                recommended_action="switch_backend",
            )

        if returncode not in (0, None) and parsed is None:
            return WorkerResult(
                backend=self.name,
                status="blocked",
                summary=f"{self.name} exited with status {returncode}.",
                next_step="Inspect stderr/stdout and adjust authentication or CLI flags.",
                evidence=[(stderr_text or stdout_text or "No subprocess output.").strip()],
                commands=_dedupe_strings(self._extract_commands_from_events(stdout_text)),
                command_events=command_events,
                event_log_path=str(events_path) if stdout_text else "",
                raw_output=raw_output,
                hypothesis="",
                hypothesis_result="untested",
                confidence=0.0,
                failure_reason="worker_error",
                recommended_action="switch_backend",
            )

        if parsed is None:
            parsed = self._parse_structured_output(raw_output)
        commands = _dedupe_strings(
            list(parsed.get("commands", [])) + self._extract_commands_from_events(stdout_text)
        )
        return WorkerResult(
            backend=self.name,
            status=parsed["status"],
            summary=parsed["summary"],
            next_step=parsed["next_step"],
            flag=parsed.get("flag"),
            evidence=list(parsed.get("evidence", [])),
            commands=commands,
            command_events=command_events,
            event_log_path=str(events_path) if stdout_text else "",
            raw_output=raw_output,
            **_extract_extended_fields(parsed),
        )

    def _build_command(
        self,
        request: WorkerRequest,
        schema_path: Path,
        output_path: Path,
        prompt: str,
    ) -> list[str]:
        command = [
            "claude",
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--no-session-persistence",
            "--json-schema",
            json.dumps(RESULT_SCHEMA),
            "--permission-mode",
            self.permission_mode,
        ]
        if self.model:
            command.extend(["--model", self.model])
        command.extend(self.extra_args)
        command.append(prompt)
        return command

    def _extract_command_events(self, event_stream: str) -> list[dict[str, Any]]:
        latest_by_id: dict[str, dict[str, Any]] = {}
        ordered_ids: list[str] = []
        for line in event_stream.splitlines():
            payload = _safe_json_loads(line)
            if not isinstance(payload, dict):
                continue

            if payload.get("type") == "assistant":
                message = payload.get("message")
                if not isinstance(message, dict):
                    continue
                for content_item in message.get("content", []):
                    if not isinstance(content_item, dict) or content_item.get("type") != "tool_use":
                        continue
                    if content_item.get("name") != "Bash":
                        continue
                    item_id = str(content_item.get("id", len(ordered_ids)))
                    latest_by_id[item_id] = {
                        "id": item_id,
                        "event_type": "tool_use",
                        "command": str(content_item.get("input", {}).get("command", "")),
                        "status": "in_progress",
                        "exit_code": None,
                        "output": "",
                    }
                    if item_id not in ordered_ids:
                        ordered_ids.append(item_id)
                continue

            if payload.get("type") != "user":
                continue
            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            for content_item in message.get("content", []):
                if not isinstance(content_item, dict) or content_item.get("type") != "tool_result":
                    continue
                item_id = str(content_item.get("tool_use_id", len(ordered_ids)))
                previous = latest_by_id.get(
                    item_id,
                    {
                        "id": item_id,
                        "event_type": "tool_result",
                        "command": "",
                        "status": "",
                        "exit_code": None,
                        "output": "",
                    },
                )
                is_error = bool(content_item.get("is_error"))
                latest_by_id[item_id] = {
                    **previous,
                    "event_type": "tool_result",
                    "status": "failed" if is_error else "completed",
                    "exit_code": 1 if is_error else 0,
                    "output": _coerce_claude_tool_result(content_item.get("content")),
                }
                if item_id not in ordered_ids:
                    ordered_ids.append(item_id)

        return [latest_by_id[item_id] for item_id in ordered_ids]

    def _emit_live_event(
        self,
        line: str,
        event_sink: Any = None,
        live_state: dict[str, str] | None = None,
    ) -> None:
        formatted = _format_claude_event_line(line)
        if formatted:
            sys.stderr.write(formatted)
            sys.stderr.flush()
        event = _extract_claude_live_command_event(line, live_state or {})
        if event and event_sink is not None:
            event_type, payload = event
            event_sink(event_type, payload)

    def _extract_claude_stream_payload(self, event_stream: str) -> dict[str, Any] | None:
        for line in reversed(event_stream.splitlines()):
            payload = _safe_json_loads(line)
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "result":
                continue
            structured_output = payload.get("structured_output")
            if isinstance(structured_output, dict):
                return structured_output
            result = payload.get("result")
            if isinstance(result, str):
                parsed = _extract_json(result)
                if isinstance(parsed, dict):
                    return parsed
        return None


def _extract_json(raw_output: str) -> Any:
    text = raw_output.strip()
    if not text:
        raise ValueError("Worker returned an empty response.")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    code_fence_match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if code_fence_match:
        return json.loads(code_fence_match.group(1))

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        return json.loads(text[first_brace : last_brace + 1])

    raise ValueError("Unable to extract JSON object from worker output.")


def extract_flag(text: str) -> str | None:
    match = FLAG_RE.search(text)
    return match.group(0) if match else None


BackendFactory = Any  # Callable[[], WorkerBackend]


_BUILTIN_BACKEND_FACTORIES: dict[str, BackendFactory] = {
    "mock": MockWorker,
    "codex": CodexWorker,
    "claude": ClaudeWorker,
}


_EXTRA_BACKEND_FACTORIES: dict[str, BackendFactory] = {}


def register_worker_backend(name: str, factory: BackendFactory) -> None:
    """Register a custom worker backend factory.

    The factory is called with no arguments and must return an instance of a
    WorkerBackend subclass. Use this to plug in Gemini, Ollama, or any other
    CLI/API backed worker without patching workers.py.
    """
    if not callable(factory):
        raise TypeError(f"backend factory for {name!r} must be callable")
    _EXTRA_BACKEND_FACTORIES[name] = factory


def build_worker_pool(backends: list[str]) -> dict[str, WorkerBackend]:
    available: dict[str, WorkerBackend] = {}
    for name, factory in _BUILTIN_BACKEND_FACTORIES.items():
        try:
            available[name] = factory()
        except Exception:
            continue
    for name, factory in _EXTRA_BACKEND_FACTORIES.items():
        try:
            available[name] = factory()
        except Exception:
            continue
    missing = [backend for backend in backends if backend not in available]
    if missing:
        known = ", ".join(sorted(available.keys())) or "<none>"
        raise KeyError(
            f"Unsupported backend(s): {', '.join(missing)}. "
            f"Known backends: {known}. "
            f"Register custom backends with register_worker_backend(name, factory)."
        )
    return {backend: available[backend] for backend in backends}


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _resolve_worker_timeout_seconds(default: int = 1800) -> int:
    raw = os.getenv("WORKER_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return default


def _streaming_popen_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {}
    return {"start_new_session": True}


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, AttributeError):
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


def _join_stream_threads(*threads: threading.Thread, timeout_seconds: float = 5.0) -> list[str]:
    warnings: list[str] = []
    for thread in threads:
        thread.join(timeout=timeout_seconds)
        if thread.is_alive():
            warnings.append(
                f"stream reader thread '{thread.name or 'unnamed'}' did not exit after {timeout_seconds:.0f}s."
            )
    return warnings


def _resolve_codex_execution_policy() -> tuple[str, str]:
    common_mode = os.getenv("WORKER_PERMISSION_MODE", "").strip().lower()
    if common_mode:
        return _map_common_permission_mode_to_codex(common_mode)
    return "workspace-write", "never"


def _resolve_claude_permission_mode() -> str:
    common_mode = os.getenv("WORKER_PERMISSION_MODE", "").strip().lower()
    if common_mode:
        return _map_common_permission_mode_to_claude(common_mode)
    return "dontAsk"


_PERMISSION_MODE_CODEX: dict[str, tuple[str, str]] = {
    "dontask": ("workspace-write", "never"),
    "dont-ask": ("workspace-write", "never"),
    "dont_ask": ("workspace-write", "never"),
    "never": ("workspace-write", "never"),
    "safe": ("workspace-write", "never"),
    "default": ("workspace-write", "never"),
    "auto": ("workspace-write", "on-request"),
    "on-request": ("workspace-write", "on-request"),
    "on_request": ("workspace-write", "on-request"),
    "plan": ("read-only", "untrusted"),
    "readonly": ("read-only", "untrusted"),
    "read-only": ("read-only", "untrusted"),
    "read_only": ("read-only", "untrusted"),
    "untrusted": ("read-only", "untrusted"),
    "bypasspermissions": ("danger-full-access", "never"),
    "bypass_permissions": ("danger-full-access", "never"),
    "danger-full-access": ("danger-full-access", "never"),
    "danger_full_access": ("danger-full-access", "never"),
    "unrestricted": ("danger-full-access", "never"),
}


_PERMISSION_MODE_CLAUDE: dict[str, str] = {
    "dontask": "dontAsk",
    "dont-ask": "dontAsk",
    "dont_ask": "dontAsk",
    "never": "dontAsk",
    "safe": "dontAsk",
    "auto": "auto",
    "on-request": "auto",
    "on_request": "auto",
    "plan": "plan",
    "readonly": "plan",
    "read-only": "plan",
    "read_only": "plan",
    "untrusted": "plan",
    "bypasspermissions": "bypassPermissions",
    "bypass_permissions": "bypassPermissions",
    "danger-full-access": "bypassPermissions",
    "danger_full_access": "bypassPermissions",
    "unrestricted": "bypassPermissions",
    "acceptedits": "acceptEdits",
    "accept_edits": "acceptEdits",
    "default": "default",
}


def _map_common_permission_mode_to_codex(value: str) -> tuple[str, str]:
    normalized = value.strip().lower()
    if normalized not in _PERMISSION_MODE_CODEX:
        valid = ", ".join(sorted(set(_PERMISSION_MODE_CODEX.keys())))
        raise ValueError(
            f"Unknown WORKER_PERMISSION_MODE value {value!r} for codex. "
            f"Valid aliases: {valid}"
        )
    return _PERMISSION_MODE_CODEX[normalized]


def _map_common_permission_mode_to_claude(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in _PERMISSION_MODE_CLAUDE:
        valid = ", ".join(sorted(set(_PERMISSION_MODE_CLAUDE.keys())))
        raise ValueError(
            f"Unknown WORKER_PERMISSION_MODE value {value!r} for claude. "
            f"Valid aliases: {valid}"
        )
    return _PERMISSION_MODE_CLAUDE[normalized]


def _read_stream_lines(
    stream: Any,
    collector: list[str],
    on_line: Any = None,
) -> None:
    if stream is None:
        return
    try:
        for line in stream:
            collector.append(line)
            if on_line is not None:
                on_line(line)
    finally:
        stream.close()


def _format_codex_event_line(line: str) -> str | None:
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "command_execution":
        return None
    command = item.get("command", "").strip()
    if not command:
        return None
    event_type = payload.get("type")
    if event_type == "item.started":
        return _prefix_worker_event_line("codex", f"start: {command}")
    if event_type == "item.completed":
        exit_code = item.get("exit_code")
        return _prefix_worker_event_line("codex", f"done ({exit_code}): {command}")
    return None


def _extract_codex_live_command_event(line: str) -> tuple[str, dict[str, Any]] | None:
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "command_execution":
        return None
    command = str(item.get("command", "")).strip()
    if not command:
        return None
    event_type = payload.get("type")
    data = {
        "backend": "codex",
        "command": command,
        "exit_code": item.get("exit_code"),
    }
    if event_type == "item.started":
        return "worker_command_started", data
    if event_type == "item.completed":
        return "worker_command_completed", data
    return None


def _format_claude_event_line(line: str) -> str | None:
    payload = _safe_json_loads(line)
    if not isinstance(payload, dict):
        return None

    if payload.get("type") == "assistant":
        message = payload.get("message")
        if not isinstance(message, dict):
            return None
        for content_item in message.get("content", []):
            if not isinstance(content_item, dict) or content_item.get("type") != "tool_use":
                continue
            if content_item.get("name") != "Bash":
                continue
            command = str(content_item.get("input", {}).get("command", "")).strip()
            if command:
                return _prefix_worker_event_line("claude", f"start: /bin/zsh -lc {command}")
        return None

    if payload.get("type") != "user":
        return None
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    for content_item in message.get("content", []):
        if not isinstance(content_item, dict) or content_item.get("type") != "tool_result":
            continue
        status = "error" if content_item.get("is_error") else "ok"
        tool_use_id = str(content_item.get("tool_use_id", ""))
        if tool_use_id:
            return _prefix_worker_event_line("claude", f"done ({status}): {tool_use_id}")
    return None


def _extract_claude_live_command_event(
    line: str,
    live_state: dict[str, str],
) -> tuple[str, dict[str, Any]] | None:
    payload = _safe_json_loads(line)
    if not isinstance(payload, dict):
        return None

    if payload.get("type") == "assistant":
        message = payload.get("message")
        if not isinstance(message, dict):
            return None
        for content_item in message.get("content", []):
            if not isinstance(content_item, dict) or content_item.get("type") != "tool_use":
                continue
            if content_item.get("name") != "Bash":
                continue
            tool_use_id = str(content_item.get("id", ""))
            command = str(content_item.get("input", {}).get("command", "")).strip()
            if not tool_use_id or not command:
                continue
            live_state[tool_use_id] = command
            return (
                "worker_command_started",
                {
                    "backend": "claude",
                    "command": f"/bin/zsh -lc {command}",
                    "exit_code": None,
                },
            )
        return None

    if payload.get("type") != "user":
        return None
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    for content_item in message.get("content", []):
        if not isinstance(content_item, dict) or content_item.get("type") != "tool_result":
            continue
        tool_use_id = str(content_item.get("tool_use_id", ""))
        command = live_state.get(tool_use_id, tool_use_id)
        return (
            "worker_command_completed",
            {
                "backend": "claude",
                "command": f"/bin/zsh -lc {command}" if not command.startswith("/bin/") else command,
                "exit_code": 1 if content_item.get("is_error") else 0,
            },
        )
    return None


def _prefix_worker_event_line(worker_name: str, message: str) -> str:
    return f"[{_current_worker_timestamp()}] [{worker_name}] {message}\n"


def _current_worker_timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _compact_attempts_for_prompt(prior_attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for attempt in prior_attempts[-3:]:
        compacted.append(
            {
                "attempt": attempt.get("attempt"),
                "backend": attempt.get("backend"),
                "status": attempt.get("status"),
                "summary": _truncate_for_prompt(str(attempt.get("summary", "")), 260),
                "next_step": _truncate_for_prompt(str(attempt.get("next_step", "")), 220),
                "evidence": [_truncate_for_prompt(str(item), 180) for item in list(attempt.get("evidence", []))[:3]],
                "key_commands": [
                    _truncate_for_prompt(str(item), 220) for item in list(attempt.get("key_commands", []))[:4]
                ],
                "inline_scripts": list(attempt.get("inline_scripts", []))[:2],
                "handoff_files": list(attempt.get("handoff_files", []))[:4],
            }
        )
    return compacted


def _truncate_for_prompt(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3].rstrip()}..."


def _safe_json_loads(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return None


def _coerce_claude_tool_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
                nested = item.get("content")
                if isinstance(nested, str):
                    parts.append(nested)
        return "\n".join(part for part in parts if part)
    return str(value)
