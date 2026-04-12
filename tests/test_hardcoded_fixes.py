import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ctf_orchestrator import agent_runtime, workers
from ctf_orchestrator.agent_runtime import (
    recent_schema_violations,
    resolve_agent_timeout,
    validate_against_schema,
)
from ctf_orchestrator.importers.sources import _HTMLTextExtractor
from ctf_orchestrator.skills import (
    _infer_category_from_slug,
    build_category_to_skill,
    load_skills,
)
from ctf_orchestrator.workers import (
    FLAG_RE,
    MockWorker,
    _map_common_permission_mode_to_claude,
    _map_common_permission_mode_to_codex,
    build_worker_pool,
    extract_flag,
    register_worker_backend,
)


ROOT = Path(__file__).resolve().parents[1]


class PermissiveFlagRegexTests(unittest.TestCase):
    def test_htb_format(self):
        self.assertEqual(extract_flag("we got HTB{sh3ll_on_host}"), "HTB{sh3ll_on_host}")

    def test_thm_format(self):
        self.assertEqual(extract_flag("THM{pwn}"), "THM{pwn}")

    def test_pico_format(self):
        self.assertEqual(extract_flag("picoCTF{abcd_1234}"), "picoCTF{abcd_1234}")

    def test_team_prefix(self):
        self.assertEqual(extract_flag("ABCD{congrats}"), "ABCD{congrats}")

    def test_legacy_flag_still_works(self):
        self.assertEqual(extract_flag("flag{legacy}"), "flag{legacy}")

    def test_ctf_prefix_still_works(self):
        self.assertEqual(extract_flag("CTF{yo}"), "CTF{yo}")

    def test_no_flag_returns_none(self):
        self.assertIsNone(extract_flag("no flag here"))

    def test_long_prefix_still_finds_substring(self):
        # Regex is a permissive scanner, not an anchor. A 30-char run of A's
        # followed by {x} is matched at offset 6 (24 trailing A's + {x}).
        too_long = "A" * 30 + "{x}"
        match = extract_flag(too_long)
        self.assertIsNotNone(match)
        self.assertTrue(match.endswith("{x}"))


class StrictPermissionModeTests(unittest.TestCase):
    def test_valid_codex_mode(self):
        sandbox, policy = _map_common_permission_mode_to_codex("plan")
        self.assertEqual(sandbox, "read-only")
        self.assertEqual(policy, "untrusted")

    def test_unknown_codex_mode_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _map_common_permission_mode_to_codex("hyperdrive")
        self.assertIn("Unknown", str(ctx.exception))
        self.assertIn("hyperdrive", str(ctx.exception))

    def test_valid_claude_mode(self):
        self.assertEqual(_map_common_permission_mode_to_claude("plan"), "plan")

    def test_unknown_claude_mode_raises(self):
        with self.assertRaises(ValueError):
            _map_common_permission_mode_to_claude("rainbow")


class ConfigurableMemoryBoundsTests(unittest.TestCase):
    def setUp(self):
        os.environ["MEMORY_TESTED_HYPOTHESES_LIMIT"] = "5"
        import importlib

        from ctf_orchestrator import graph as graph_module

        importlib.reload(graph_module)
        self.graph_module = graph_module

    def tearDown(self):
        os.environ.pop("MEMORY_TESTED_HYPOTHESES_LIMIT", None)
        import importlib

        from ctf_orchestrator import graph as graph_module

        importlib.reload(graph_module)

    def test_env_override_applied(self):
        self.assertEqual(self.graph_module.MEMORY_TESTED_HYPOTHESES_LIMIT, 5)


class BackendRegistryTests(unittest.TestCase):
    def tearDown(self):
        workers._EXTRA_BACKEND_FACTORIES.clear()

    def test_register_and_build_custom_backend(self):
        register_worker_backend("custom-mock", MockWorker)
        pool = build_worker_pool(["custom-mock"])
        self.assertIn("custom-mock", pool)
        self.assertIsInstance(pool["custom-mock"], MockWorker)

    def test_unknown_backend_error_lists_registered(self):
        with self.assertRaises(KeyError) as ctx:
            build_worker_pool(["hyperpwn"])
        self.assertIn("Known backends", str(ctx.exception))
        self.assertIn("mock", str(ctx.exception))


class SkillsAutoDiscoveryTests(unittest.TestCase):
    def test_infer_category_from_slug_variants(self):
        self.assertEqual(_infer_category_from_slug("ctf-web-solver"), "web")
        self.assertEqual(_infer_category_from_slug("ctf-blockchain-solver"), "blockchain")
        self.assertIsNone(_infer_category_from_slug("custom-prefix-solver"))

    def test_build_mapping_from_skills_root(self):
        skills = load_skills(ROOT / "skills")
        mapping = build_category_to_skill(skills)
        # Core categories must be present from the default map or inferred.
        for category in ("web", "crypto", "pwn", "reverse", "misc"):
            self.assertIn(category, mapping)

    def test_custom_category_front_matter(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ctf-ai-adversarial-solver").mkdir()
            (root / "ctf-ai-adversarial-solver" / "SKILL.md").write_text(
                "---\nname: ctf-ai-adversarial-solver\ndescription: AI adv\ncategory: ai_adversarial\n---\n# x\n",
                encoding="utf-8",
            )
            skills = load_skills(root)
            mapping = build_category_to_skill(skills)
            self.assertIn("ai_adversarial", mapping)
            self.assertEqual(mapping["ai_adversarial"], "ctf-ai-adversarial-solver")


class HTMLExtractorTests(unittest.TestCase):
    def test_pre_code_preserved(self):
        html = "<p>intro</p><pre><code>def x():\n    return 1</code></pre><p>after</p>"
        parser = _HTMLTextExtractor(base_url="https://e.com")
        parser.feed(html)
        text = parser.text()
        self.assertIn("def x():", text)
        self.assertIn("    return 1", text)

    def test_script_and_style_dropped(self):
        html = "<p>visible</p><script>alert(1)</script><style>body{}</style>"
        parser = _HTMLTextExtractor(base_url="https://e.com")
        parser.feed(html)
        text = parser.text()
        self.assertIn("visible", text)
        self.assertNotIn("alert(1)", text)
        self.assertNotIn("body{}", text)

    def test_table_cells_separated(self):
        html = "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>"
        parser = _HTMLTextExtractor(base_url="https://e.com")
        parser.feed(html)
        text = parser.text()
        self.assertIn("a", text)
        self.assertIn("b", text)
        self.assertIn("c", text)
        self.assertIn("d", text)


class PerRoleTimeoutTests(unittest.TestCase):
    def setUp(self):
        for key in ("AGENT_TIMEOUT_DEFAULT", "AGENT_TIMEOUT_SUMMARIZER", "AGENT_TIMEOUT_WRITEUP"):
            os.environ.pop(key, None)

    def tearDown(self):
        self.setUp()

    def test_default_fallback(self):
        self.assertEqual(resolve_agent_timeout("summarizer", default=200), 200)

    def test_role_env_wins(self):
        os.environ["AGENT_TIMEOUT_SUMMARIZER"] = "77"
        self.assertEqual(resolve_agent_timeout("summarizer"), 77)

    def test_default_env_fallback(self):
        os.environ["AGENT_TIMEOUT_DEFAULT"] = "42"
        self.assertEqual(resolve_agent_timeout("writeup"), 42)

    def test_role_env_beats_default_env(self):
        os.environ["AGENT_TIMEOUT_DEFAULT"] = "42"
        os.environ["AGENT_TIMEOUT_WRITEUP"] = "99"
        self.assertEqual(resolve_agent_timeout("writeup"), 99)


class ExtensibleEnumValidationTests(unittest.TestCase):
    def test_unknown_enum_accepted_leniently(self):
        schema = {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["a", "b", "c"]},
            },
            "required": ["decision"],
        }
        # This used to raise; now it should log and accept.
        before = len(recent_schema_violations())
        validate_against_schema({"decision": "novel_value"}, schema)
        after = len(recent_schema_violations())
        self.assertGreater(after, before)

    def test_strict_mode_still_raises(self):
        schema = {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["a", "b"]},
            },
            "required": ["decision"],
        }
        with self.assertRaises(Exception):
            validate_against_schema({"decision": "c"}, schema, lenient_enums=False)


class RunsPathConfigTests(unittest.TestCase):
    def test_runs_dir_override(self):
        os.environ["CTF_RUNS_DIR"] = "custom-runs"
        try:
            import importlib

            from ctf_orchestrator import graph as graph_module

            importlib.reload(graph_module)
            self.assertEqual(graph_module.RUNS_DIR, "custom-runs")
            self.assertEqual(graph_module.ATTEMPT_HISTORY_PATH, "custom-runs/attempt-history.json")
        finally:
            os.environ.pop("CTF_RUNS_DIR", None)
            import importlib

            from ctf_orchestrator import graph as graph_module

            importlib.reload(graph_module)


if __name__ == "__main__":
    unittest.main()
