"""Unified agent dispatch runtime.

Consolidates what used to be four near-duplicate subprocess wrappers (writeups,
memory, import_agent, supervisor_agent). Adds:

- budget tracking (hard cap on LLM subprocess spawns)
- concurrency semaphore shared across agent calls
- sha1-keyed on-disk cache for idempotent roles (writeup, memory, import list)
- structured .runs/llm-calls.jsonl audit log
- role-based model routing via CLAUDE_MODEL_<ROLE> / CODEX_MODEL_<ROLE>
- robust JSON extraction + minimal schema validation
- mock agent backend for offline testing
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import textwrap
import threading
from typing import Any, Callable


DEFAULT_AGENT_TIMEOUT_SECONDS = 300


def resolve_agent_timeout(role: str, default: int | None = None) -> int:
    """Resolve the timeout for a given agent role.

    Hierarchy: AGENT_TIMEOUT_<ROLE> → AGENT_TIMEOUT_DEFAULT → explicit default
    → DEFAULT_AGENT_TIMEOUT_SECONDS.
    """
    role_key = role.upper().replace("-", "_").replace(".", "_")
    for name in (f"AGENT_TIMEOUT_{role_key}", "AGENT_TIMEOUT_DEFAULT"):
        raw = os.getenv(name, "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    if default is not None and default > 0:
        return default
    return DEFAULT_AGENT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Budget + concurrency
# ---------------------------------------------------------------------------


class LLMBudgetExceeded(RuntimeError):
    pass


class _BudgetTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls = 0
        self._limit = _resolve_budget_limit()

    def check_and_increment(self) -> None:
        with self._lock:
            if self._limit is not None and self._calls >= self._limit:
                raise LLMBudgetExceeded(
                    f"LLM budget exhausted: {self._calls}/{self._limit} calls"
                )
            self._calls += 1

    def count(self) -> int:
        with self._lock:
            return self._calls

    def reset_for_tests(self) -> None:
        with self._lock:
            self._calls = 0
            self._limit = _resolve_budget_limit()


def _resolve_budget_limit() -> int | None:
    raw = os.getenv("CTF_LLM_BUDGET_MAX_CALLS", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


BUDGET = _BudgetTracker()


def _resolve_concurrency() -> int:
    raw = os.getenv("CTF_LLM_MAX_CONCURRENCY", "3").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 3
    return max(1, value)


_AGENT_SEMAPHORE = threading.BoundedSemaphore(_resolve_concurrency())


# ---------------------------------------------------------------------------
# Minimal JSON Schema validator (stdlib only)
# ---------------------------------------------------------------------------


class SchemaValidationError(ValueError):
    pass


_SCHEMA_VIOLATIONS: list[dict[str, Any]] = []
_SCHEMA_VIOLATIONS_LOCK = threading.Lock()


def record_schema_violation(path: str, detail: str) -> None:
    with _SCHEMA_VIOLATIONS_LOCK:
        _SCHEMA_VIOLATIONS.append({"path": path, "detail": detail})
        if len(_SCHEMA_VIOLATIONS) > 256:
            del _SCHEMA_VIOLATIONS[:128]


def recent_schema_violations() -> list[dict[str, Any]]:
    with _SCHEMA_VIOLATIONS_LOCK:
        return list(_SCHEMA_VIOLATIONS)


def validate_against_schema(
    payload: Any,
    schema: dict[str, Any],
    path: str = "$",
    *,
    lenient_enums: bool = True,
) -> None:
    """Validate an already-parsed JSON value against a subset of JSON Schema.

    Supports: type, nullable unions, required, properties, additionalProperties,
    items, enum, minimum, maximum. Sufficient for every schema used in this
    project.

    When `lenient_enums` is True (default), unknown enum values are logged via
    `record_schema_violation` and accepted instead of raising. This prevents a
    single novel `recommended_action` value from nuking the whole dispatch;
    callers can still inspect the recorded violations for observability.
    """
    expected_type = schema.get("type")
    if expected_type is not None:
        if not _matches_type(payload, expected_type):
            raise SchemaValidationError(
                f"{path}: expected type {expected_type!r}, got {type(payload).__name__}"
            )

    enum_values = schema.get("enum")
    if enum_values is not None and payload not in enum_values:
        if lenient_enums:
            record_schema_violation(path, f"unknown enum value {payload!r} not in {enum_values}")
        else:
            raise SchemaValidationError(f"{path}: value {payload!r} not in enum {enum_values}")

    if isinstance(payload, dict):
        required = schema.get("required", [])
        for field_name in required:
            if field_name not in payload:
                raise SchemaValidationError(f"{path}: missing required field {field_name!r}")
        properties = schema.get("properties", {}) or {}
        for key, value in payload.items():
            if key in properties:
                validate_against_schema(value, properties[key], f"{path}.{key}", lenient_enums=lenient_enums)
            elif schema.get("additionalProperties") is False:
                if lenient_enums:
                    record_schema_violation(f"{path}.{key}", f"unexpected field {key!r}")
                else:
                    raise SchemaValidationError(f"{path}: unexpected field {key!r}")

    if isinstance(payload, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(payload):
                validate_against_schema(item, item_schema, f"{path}[{index}]", lenient_enums=lenient_enums)

    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and payload < minimum:
            raise SchemaValidationError(f"{path}: value {payload} below minimum {minimum}")
        if maximum is not None and payload > maximum:
            raise SchemaValidationError(f"{path}: value {payload} above maximum {maximum}")


def _matches_type(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_matches_type(value, item) for item in expected)
    mapping = {
        "string": str,
        "integer": int,
        "boolean": bool,
        "array": list,
        "object": dict,
        "null": type(None),
    }
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    py_type = mapping.get(expected)
    if py_type is None:
        return True
    return isinstance(value, py_type)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


class JSONExtractionError(ValueError):
    pass


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def extract_json(raw_output: str) -> Any:
    """Best-effort JSON extraction from mixed LLM output.

    Strategy:
    1. Direct json.loads on the full text.
    2. Triple-fenced block (```json ... ```).
    3. Balanced-brace scan from first `{` forward, counting braces/brackets while
       respecting string literals.
    """
    text = (raw_output or "").strip()
    if not text:
        raise JSONExtractionError("empty LLM response")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fence_match = _FENCE_RE.search(text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    candidate = _scan_balanced_json(text)
    if candidate is not None:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise JSONExtractionError(
                f"balanced JSON candidate did not parse: {exc}. Prefix: {text[:200]!r}"
            ) from exc

    raise JSONExtractionError(
        f"no JSON object located in LLM output. Prefix: {text[:200]!r}"
    )


def _scan_balanced_json(text: str) -> str | None:
    start = -1
    for index, char in enumerate(text):
        if char in "{[":
            start = index
            break
    if start < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


# ---------------------------------------------------------------------------
# Mock agent backend
# ---------------------------------------------------------------------------


MockResponder = Callable[["AgentInvocation"], dict[str, Any]]

_MOCK_RESPONDERS: dict[str, MockResponder] = {}
_MOCK_DEFAULT: dict[str, dict[str, Any]] = {}


def register_mock_responder(role: str, responder: MockResponder) -> None:
    _MOCK_RESPONDERS[role] = responder


def register_mock_default(role: str, response: dict[str, Any]) -> None:
    _MOCK_DEFAULT[role] = response


def clear_mock_registry() -> None:
    _MOCK_RESPONDERS.clear()
    _MOCK_DEFAULT.clear()


def is_mock_enabled() -> bool:
    value = os.getenv("CTF_AGENTS_MOCK", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------


def resolve_model(role: str, backend_name: str, default: str) -> str:
    role_key = role.upper().replace("-", "_").replace(".", "_")
    candidates: list[str] = []
    if backend_name == "claude":
        candidates += [f"CLAUDE_MODEL_{role_key}", "CLAUDE_MODEL"]
    elif backend_name == "codex":
        candidates += [f"CODEX_MODEL_{role_key}", "CODEX_MODEL"]
    for name in candidates:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentInvocation:
    role: str  # "writeup" | "memory" | "import" | "supervisor" | "summarizer" | ...
    skill_slug: str
    prompt: str
    schema: dict[str, Any]
    workspace: Path
    backend_sequence: list[str]
    cache_key: str | None = None  # sha1 string; None disables caching
    timeout_seconds: int = DEFAULT_AGENT_TIMEOUT_SECONDS
    call_log_dir: Path | None = None  # defaults to workspace/.runs


@dataclass
class AgentInvocationResult:
    ok: bool
    payload: dict[str, Any] | None
    raw_output: str
    backend: str
    role: str
    cached: bool = False
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def invoke_agent(
    invocation: AgentInvocation,
    *,
    workers: dict[str, Any],
) -> AgentInvocationResult:
    """Dispatch an agent call through the unified runtime."""
    cache_path = _cache_path(invocation)
    if cache_path is not None and cache_path.exists():
        try:
            cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
            _record_call_log(invocation, "claude", 0.0, 0, "cached", None)
            return AgentInvocationResult(
                ok=True,
                payload=cached_payload,
                raw_output=json.dumps(cached_payload, ensure_ascii=False),
                backend="cache",
                role=invocation.role,
                cached=True,
            )
        except Exception:
            pass

    if is_mock_enabled():
        result = _invoke_mock(invocation)
        if (
            result.ok
            and cache_path is not None
            and isinstance(result.payload, dict)
        ):
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps(result.payload, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass
        return result

    try:
        BUDGET.check_and_increment()
    except LLMBudgetExceeded as exc:
        _record_call_log(invocation, "budget", 0.0, 0, "budget_exceeded", str(exc))
        return AgentInvocationResult(
            ok=False,
            payload=None,
            raw_output="",
            backend="budget",
            role=invocation.role,
            error=str(exc),
        )

    backend_name = _select_backend(invocation.backend_sequence, workers)
    if backend_name is None:
        _record_call_log(invocation, "none", 0.0, 0, "no_backend", "no LLM backend available")
        return AgentInvocationResult(
            ok=False,
            payload=None,
            raw_output="",
            backend="none",
            role=invocation.role,
            error="no LLM backend available",
        )

    worker = workers[backend_name]
    probe_cli_versions(workspace=invocation.workspace)
    started = datetime.now()
    with _AGENT_SEMAPHORE:
        raw_output = _dispatch_subprocess(
            backend_name=backend_name,
            worker=worker,
            invocation=invocation,
        )
    elapsed = (datetime.now() - started).total_seconds()

    if not raw_output:
        _record_call_log(invocation, backend_name, elapsed, 0, "empty", None)
        return AgentInvocationResult(
            ok=False,
            payload=None,
            raw_output="",
            backend=backend_name,
            role=invocation.role,
            error="empty response from backend",
        )

    try:
        parsed = extract_json(raw_output)
    except JSONExtractionError as exc:
        _record_call_log(invocation, backend_name, elapsed, len(raw_output), "extract_failed", str(exc))
        return AgentInvocationResult(
            ok=False,
            payload=None,
            raw_output=raw_output,
            backend=backend_name,
            role=invocation.role,
            error=f"json extraction failed: {exc}",
        )

    if not isinstance(parsed, (dict, list)):
        _record_call_log(invocation, backend_name, elapsed, len(raw_output), "wrong_shape", None)
        return AgentInvocationResult(
            ok=False,
            payload=None,
            raw_output=raw_output,
            backend=backend_name,
            role=invocation.role,
            error=f"expected object/array, got {type(parsed).__name__}",
        )

    payload_dict = parsed if isinstance(parsed, dict) else {"items": parsed}
    try:
        validate_against_schema(parsed, invocation.schema)
    except SchemaValidationError as exc:
        _record_call_log(invocation, backend_name, elapsed, len(raw_output), "schema_invalid", str(exc))
        return AgentInvocationResult(
            ok=False,
            payload=payload_dict,
            raw_output=raw_output,
            backend=backend_name,
            role=invocation.role,
            error=f"schema validation failed: {exc}",
        )

    _record_call_log(invocation, backend_name, elapsed, len(raw_output), "ok", None)

    if cache_path is not None and isinstance(parsed, dict):
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    return AgentInvocationResult(
        ok=True,
        payload=payload_dict,
        raw_output=raw_output,
        backend=backend_name,
        role=invocation.role,
    )


def _invoke_mock(invocation: AgentInvocation) -> AgentInvocationResult:
    responder = _MOCK_RESPONDERS.get(invocation.role)
    if responder is not None:
        try:
            payload = responder(invocation)
        except Exception as exc:
            return AgentInvocationResult(
                ok=False,
                payload=None,
                raw_output="",
                backend="mock",
                role=invocation.role,
                error=f"mock responder raised: {exc}",
            )
    else:
        payload = _MOCK_DEFAULT.get(invocation.role, {})
    try:
        validate_against_schema(payload, invocation.schema)
    except SchemaValidationError as exc:
        return AgentInvocationResult(
            ok=False,
            payload=payload,
            raw_output=json.dumps(payload),
            backend="mock",
            role=invocation.role,
            error=f"mock payload invalid: {exc}",
        )
    _record_call_log(invocation, "mock", 0.0, 0, "mock", None)
    return AgentInvocationResult(
        ok=True,
        payload=payload,
        raw_output=json.dumps(payload),
        backend="mock",
        role=invocation.role,
    )


def _select_backend(backend_sequence: list[str], workers: dict[str, Any]) -> str | None:
    from .workers import ClaudeWorker, CodexWorker

    preference = [name for name in backend_sequence if name in workers]
    for fallback in ("claude", "codex"):
        if fallback in workers and fallback not in preference:
            preference.append(fallback)
    for name in preference:
        worker = workers.get(name)
        if isinstance(worker, (ClaudeWorker, CodexWorker)):
            return name
    return None


def _dispatch_subprocess(
    *,
    backend_name: str,
    worker: Any,
    invocation: AgentInvocation,
) -> str:
    from .workers import ClaudeWorker, CodexWorker

    per_role_timeout = resolve_agent_timeout(invocation.role, default=invocation.timeout_seconds)
    worker_timeout = getattr(worker, "timeout_seconds", per_role_timeout)
    timeout = max(1, min(per_role_timeout, worker_timeout))
    workspace = invocation.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    run_dir = workspace / ".runs" / invocation.role
    run_dir.mkdir(parents=True, exist_ok=True)

    if isinstance(worker, ClaudeWorker):
        model = resolve_model(invocation.role, "claude", getattr(worker, "model", "") or "")
        command = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--json-schema",
            json.dumps(invocation.schema),
            "--permission-mode",
            getattr(worker, "permission_mode", "dontAsk"),
        ]
        if model:
            command.extend(["--model", model])
        command.extend(list(getattr(worker, "extra_args", []) or []))
        command.append(invocation.prompt)
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ""
        return (completed.stdout or "").strip() or (completed.stderr or "").strip()

    if isinstance(worker, CodexWorker):
        model = resolve_model(invocation.role, "codex", getattr(worker, "model", "") or "")
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            schema_path = tmp_path / "schema.json"
            output_path = tmp_path / "output.json"
            schema_path.write_text(json.dumps(invocation.schema), encoding="utf-8")
            command = [
                "codex",
                "-a",
                getattr(worker, "approval_policy", "never"),
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                getattr(worker, "sandbox", "workspace-write"),
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
                "-C",
                str(workspace),
            ]
            if model:
                command[1:1] = ["-m", model]
            command = command + list(getattr(worker, "extra_args", []) or []) + ["-"]
            try:
                completed = subprocess.run(
                    command,
                    cwd=workspace,
                    input=invocation.prompt,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return ""
            if output_path.exists():
                raw = output_path.read_text(encoding="utf-8").strip()
                if raw:
                    return raw
            return (completed.stdout or "").strip() or (completed.stderr or "").strip()

    return ""


def _cache_path(invocation: AgentInvocation) -> Path | None:
    if invocation.cache_key is None:
        return None
    role = invocation.role
    return invocation.workspace / ".runs" / "cache" / role / f"{invocation.cache_key}.json"


def _record_call_log(
    invocation: AgentInvocation,
    backend: str,
    elapsed_seconds: float,
    raw_len: int,
    status: str,
    error: str | None,
) -> None:
    try:
        log_dir = invocation.call_log_dir or invocation.workspace / ".runs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "llm-calls.jsonl"
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "role": invocation.role,
            "skill": invocation.skill_slug,
            "backend": backend,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "raw_output_length": raw_len,
            "status": status,
            "budget_calls_used": BUDGET.count(),
        }
        if error:
            entry["error"] = error
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


SUMMARIZER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_findings": {"type": "array", "items": {"type": "string"}},
        "notable_commands": {"type": "array", "items": {"type": "string"}},
        "dropped_context_hint": {"type": "string"},
    },
    "required": ["summary", "key_findings"],
}


def summarizer_threshold() -> int:
    raw = os.getenv("WORKER_SUMMARIZER_THRESHOLD", "8000").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 8000
    return max(0, value)


def summarizer_enabled() -> bool:
    value = os.getenv("WORKER_SUMMARIZER_DISABLED", "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def summarize_worker_output(
    *,
    workspace: Path,
    workers: dict[str, Any],
    backend_sequence: list[str],
    role_context: str,
    raw_output: str,
) -> str:
    """Compress a long worker stdout blob via a cheap summarizer agent call.

    Falls back to a deterministic head+tail trim on any failure so the rest of
    the pipeline keeps running.
    """
    if not summarizer_enabled():
        return raw_output
    threshold = summarizer_threshold()
    if threshold <= 0 or len(raw_output) <= threshold:
        return raw_output
    prompt = textwrap.dedent(
        f"""
        Role context: {role_context}
        You are a cheap post-hoc summarizer that compresses raw worker output
        so the next orchestration step does not explode the context window.

        Requirements:
        - Preserve any candidate flags, credentials, hostnames, tokens.
        - Preserve the exact commands that were executed, up to 10 items.
        - Preserve concrete error messages that triggered the worker to stop.
        - Drop noisy tool boilerplate, repeated lines, stack traces without info.
        - Return STRICT JSON matching the schema. No markdown, no prose.

        Raw worker output:
        ---
        {raw_output[:40000]}
        ---
        """
    ).strip()
    invocation = AgentInvocation(
        role="summarizer",
        skill_slug="ctf-summarizer-inline",
        prompt=prompt,
        schema=SUMMARIZER_SCHEMA,
        workspace=workspace,
        backend_sequence=backend_sequence,
        timeout_seconds=120,
    )
    result = invoke_agent(invocation, workers=workers)
    if not result.ok or not isinstance(result.payload, dict):
        return _deterministic_trim(raw_output, threshold)
    summary = str(result.payload.get("summary") or "").strip()
    findings = result.payload.get("key_findings") or []
    commands = result.payload.get("notable_commands") or []
    parts = [f"[summarized from {len(raw_output)} chars]"]
    if summary:
        parts.append(summary)
    if findings:
        parts.append("Key findings:\n- " + "\n- ".join(str(item) for item in findings[:12]))
    if commands:
        parts.append("Notable commands:\n- " + "\n- ".join(str(item) for item in commands[:10]))
    return "\n\n".join(parts)


def _deterministic_trim(raw_output: str, threshold: int) -> str:
    head = raw_output[: threshold // 2]
    tail = raw_output[-threshold // 2 :]
    return f"{head}\n... [trimmed {len(raw_output) - threshold} chars] ...\n{tail}"


_CLI_VERSION_CACHE: dict[str, str] = {}
_CLI_VERSION_LOCK = threading.Lock()


def probe_cli_versions(workspace: Path | None = None) -> dict[str, str]:
    """Probe installed LLM CLIs for their version string (observability only).

    Non-blocking. Each probe has a short timeout. Results are cached in-process.
    On first call the versions are also appended to llm-calls.jsonl for audit.
    """
    with _CLI_VERSION_LOCK:
        if _CLI_VERSION_CACHE:
            return dict(_CLI_VERSION_CACHE)

        for cli in ("claude", "codex"):
            try:
                completed = subprocess.run(
                    [cli, "--version"],
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                version = (completed.stdout or completed.stderr or "").strip().splitlines()[0] if (completed.stdout or completed.stderr) else ""
                _CLI_VERSION_CACHE[cli] = version or "unknown"
            except FileNotFoundError:
                _CLI_VERSION_CACHE[cli] = "not-installed"
            except Exception as exc:
                _CLI_VERSION_CACHE[cli] = f"probe-failed: {exc}"[:120]

        if workspace is not None:
            try:
                log_dir = workspace / ".runs"
                log_dir.mkdir(parents=True, exist_ok=True)
                entry = {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "role": "cli_probe",
                    "versions": dict(_CLI_VERSION_CACHE),
                }
                with (log_dir / "llm-calls.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass
        return dict(_CLI_VERSION_CACHE)


def compute_cache_key(*parts: Any) -> str:
    hasher = hashlib.sha1()
    for part in parts:
        if isinstance(part, (dict, list)):
            hasher.update(json.dumps(part, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        else:
            hasher.update(str(part).encode("utf-8"))
        hasher.update(b"\x1e")
    return hasher.hexdigest()[:16]
