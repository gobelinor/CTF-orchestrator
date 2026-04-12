import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ctf_orchestrator import agent_runtime
from ctf_orchestrator.agent_runtime import (
    AgentInvocation,
    BUDGET,
    JSONExtractionError,
    LLMBudgetExceeded,
    SchemaValidationError,
    compute_cache_key,
    extract_json,
    invoke_agent,
    register_mock_default,
    register_mock_responder,
    resolve_model,
    summarize_worker_output,
    validate_against_schema,
)


class ExtractJsonTests(unittest.TestCase):
    def test_plain_json_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_block(self):
        self.assertEqual(extract_json("```json\n{\"a\": 2}\n```"), {"a": 2})

    def test_balanced_scan_recovers_nested(self):
        raw = "noise before {\"outer\": {\"inner\": [1, 2]}} noise after"
        self.assertEqual(extract_json(raw), {"outer": {"inner": [1, 2]}})

    def test_escaped_brace_in_string(self):
        raw = "prefix {\"text\": \"has }\\\" brace\"} trailing"
        self.assertEqual(extract_json(raw), {"text": "has }\" brace"})

    def test_empty_raises(self):
        with self.assertRaises(JSONExtractionError):
            extract_json("")

    def test_no_json_raises(self):
        with self.assertRaises(JSONExtractionError):
            extract_json("no braces here")


class SchemaValidationTests(unittest.TestCase):
    SCHEMA = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer", "minimum": 0},
            "tags": {"type": "array", "items": {"type": "string"}},
            "kind": {"type": "string", "enum": ["a", "b"]},
        },
        "required": ["name", "age"],
        "additionalProperties": False,
    }

    def test_valid(self):
        validate_against_schema(
            {"name": "x", "age": 3, "tags": ["t"], "kind": "a"}, self.SCHEMA
        )

    def test_missing_required(self):
        with self.assertRaises(SchemaValidationError):
            validate_against_schema({"name": "x"}, self.SCHEMA)

    def test_wrong_type(self):
        with self.assertRaises(SchemaValidationError):
            validate_against_schema({"name": "x", "age": "not-int"}, self.SCHEMA)

    def test_enum_violation_strict(self):
        with self.assertRaises(SchemaValidationError):
            validate_against_schema(
                {"name": "x", "age": 1, "kind": "z"}, self.SCHEMA, lenient_enums=False
            )

    def test_enum_violation_lenient_logs(self):
        # Default lenient mode accepts unknown enum values but records them.
        from ctf_orchestrator.agent_runtime import recent_schema_violations

        before = len(recent_schema_violations())
        validate_against_schema({"name": "x", "age": 1, "kind": "novel"}, self.SCHEMA)
        after = len(recent_schema_violations())
        self.assertGreater(after, before)

    def test_additional_property_strict(self):
        with self.assertRaises(SchemaValidationError):
            validate_against_schema(
                {"name": "x", "age": 1, "extra": True}, self.SCHEMA, lenient_enums=False
            )

    def test_nullable_union(self):
        schema = {"type": "object", "properties": {"x": {"type": ["string", "null"]}}, "required": ["x"]}
        validate_against_schema({"x": None}, schema)
        validate_against_schema({"x": "value"}, schema)
        with self.assertRaises(SchemaValidationError):
            validate_against_schema({"x": 1}, schema)


class ModelRoutingTests(unittest.TestCase):
    def setUp(self):
        self._backup = {
            key: os.environ.pop(key, None)
            for key in (
                "CLAUDE_MODEL",
                "CLAUDE_MODEL_SUPERVISOR",
                "CLAUDE_MODEL_SOLVER",
                "CODEX_MODEL",
                "CODEX_MODEL_IMPORT",
            )
        }

    def tearDown(self):
        for key, value in self._backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_specific_wins(self):
        os.environ["CLAUDE_MODEL"] = "default"
        os.environ["CLAUDE_MODEL_SUPERVISOR"] = "haiku"
        self.assertEqual(resolve_model("supervisor", "claude", "fallback"), "haiku")

    def test_fallback_to_claude_model(self):
        os.environ["CLAUDE_MODEL"] = "sonnet"
        self.assertEqual(resolve_model("solver", "claude", "fallback"), "sonnet")

    def test_default_when_no_env(self):
        self.assertEqual(resolve_model("memory", "claude", "fallback"), "fallback")

    def test_codex_role(self):
        os.environ["CODEX_MODEL_IMPORT"] = "gpt-mini"
        self.assertEqual(resolve_model("import", "codex", "fallback"), "gpt-mini")


class MockAgentTests(unittest.TestCase):
    def setUp(self):
        os.environ["CTF_AGENTS_MOCK"] = "1"
        register_mock_default(
            "test",
            {"a": "ok"},
        )

    def tearDown(self):
        os.environ.pop("CTF_AGENTS_MOCK", None)
        agent_runtime.clear_mock_registry()

    def test_mock_default_response(self):
        with TemporaryDirectory() as tmp:
            result = invoke_agent(
                AgentInvocation(
                    role="test",
                    skill_slug="skill",
                    prompt="p",
                    schema={"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                    workspace=Path(tmp),
                    backend_sequence=["claude"],
                ),
                workers={},
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.payload, {"a": "ok"})
        self.assertEqual(result.backend, "mock")

    def test_mock_responder_dynamic(self):
        register_mock_responder("dyn", lambda inv: {"echo": inv.prompt[:3]})
        with TemporaryDirectory() as tmp:
            result = invoke_agent(
                AgentInvocation(
                    role="dyn",
                    skill_slug="skill",
                    prompt="abcdef",
                    schema={"type": "object", "properties": {"echo": {"type": "string"}}, "required": ["echo"]},
                    workspace=Path(tmp),
                    backend_sequence=["claude"],
                ),
                workers={},
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["echo"], "abc")


class CacheTests(unittest.TestCase):
    def setUp(self):
        os.environ["CTF_AGENTS_MOCK"] = "1"
        register_mock_default("cached", {"k": "v"})
        self.calls = 0

        def counting_responder(invocation):
            self.calls += 1
            return {"k": "v"}

        register_mock_responder("cached", counting_responder)

    def tearDown(self):
        os.environ.pop("CTF_AGENTS_MOCK", None)
        agent_runtime.clear_mock_registry()

    def test_cache_stores_response(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            schema = {"type": "object", "properties": {"k": {"type": "string"}}, "required": ["k"]}
            invocation = AgentInvocation(
                role="cached",
                skill_slug="s",
                prompt="p",
                schema=schema,
                workspace=workspace,
                backend_sequence=["claude"],
                cache_key=compute_cache_key("k1"),
            )
            first = invoke_agent(invocation, workers={})
            self.assertTrue(first.ok)
            self.assertFalse(first.cached)
            second = invoke_agent(invocation, workers={})
            self.assertTrue(second.ok)
            self.assertTrue(second.cached)
            self.assertEqual(second.backend, "cache")


class BudgetTests(unittest.TestCase):
    def setUp(self):
        os.environ["CTF_LLM_BUDGET_MAX_CALLS"] = "1"
        os.environ["CTF_AGENTS_MOCK"] = ""
        BUDGET.reset_for_tests()

    def tearDown(self):
        os.environ.pop("CTF_LLM_BUDGET_MAX_CALLS", None)
        os.environ.pop("CTF_AGENTS_MOCK", None)
        BUDGET.reset_for_tests()

    def test_budget_blocks_second_call(self):
        BUDGET.check_and_increment()
        with self.assertRaises(LLMBudgetExceeded):
            BUDGET.check_and_increment()


class SummarizerTests(unittest.TestCase):
    def setUp(self):
        os.environ["WORKER_SUMMARIZER_THRESHOLD"] = "100"
        os.environ["CTF_AGENTS_MOCK"] = "1"
        register_mock_default(
            "summarizer",
            {
                "summary": "compressed",
                "key_findings": ["a", "b"],
                "notable_commands": ["curl"],
                "dropped_context_hint": "",
            },
        )

    def tearDown(self):
        os.environ.pop("WORKER_SUMMARIZER_THRESHOLD", None)
        os.environ.pop("CTF_AGENTS_MOCK", None)
        agent_runtime.clear_mock_registry()

    def test_short_output_unchanged(self):
        with TemporaryDirectory() as tmp:
            out = summarize_worker_output(
                workspace=Path(tmp),
                workers={},
                backend_sequence=["claude"],
                role_context="solver",
                raw_output="short",
            )
            self.assertEqual(out, "short")

    def test_long_output_summarized(self):
        with TemporaryDirectory() as tmp:
            raw = "x" * 5000
            out = summarize_worker_output(
                workspace=Path(tmp),
                workers={},
                backend_sequence=["claude"],
                role_context="solver",
                raw_output=raw,
            )
            self.assertIn("compressed", out)
            self.assertIn("Key findings", out)


if __name__ == "__main__":
    unittest.main()
