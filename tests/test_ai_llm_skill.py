import unittest
from pathlib import Path

from ctf_orchestrator.skills import (
    CATEGORY_ALIASES,
    _DEFAULT_CATEGORY_TO_SKILL,
    build_category_to_skill,
    load_skills,
    resolve_specialist_skill,
    route_category,
)


ROOT = Path(__file__).resolve().parents[1]


class AILLMSkillTests(unittest.TestCase):
    def setUp(self):
        self.skills = load_skills(ROOT / "skills")

    def test_skill_loaded(self):
        self.assertIn("ctf-ai-llm-solver", self.skills)
        skill = self.skills["ctf-ai-llm-solver"]
        self.assertIn("prompt injection", skill.description.lower())
        self.assertIn("Graphiti", skill.instructions)
        self.assertIn("ecole2600_securite_ia", skill.instructions)

    def test_category_alias_resolves(self):
        self.assertEqual(CATEGORY_ALIASES.get("ai"), "ai_llm")
        self.assertEqual(CATEGORY_ALIASES.get("llm"), "ai_llm")
        self.assertEqual(CATEGORY_ALIASES.get("prompt injection"), "ai_llm")
        self.assertEqual(CATEGORY_ALIASES.get("jailbreak"), "ai_llm")

    def test_default_mapping_has_ai_llm(self):
        self.assertEqual(_DEFAULT_CATEGORY_TO_SKILL["ai_llm"], "ctf-ai-llm-solver")

    def test_build_mapping_includes_ai_llm(self):
        mapping = build_category_to_skill(self.skills)
        self.assertEqual(mapping["ai_llm"], "ctf-ai-llm-solver")

    def test_resolve_specialist_returns_ai_skill(self):
        skill = resolve_specialist_skill("ai_llm", self.skills)
        self.assertEqual(skill.slug, "ctf-ai-llm-solver")

    def test_route_category_from_prompt_injection_text(self):
        category, reason = route_category(
            "We have a chatbot, inject a prompt to leak the system prompt",
            llm_fallback=False,
        )
        self.assertEqual(category, "ai_llm")
        self.assertIn("ai_llm", reason)

    def test_route_category_from_safetensors_text(self):
        category, _ = route_category(
            "Audit this safetensors file for a hidden flag in latent embeddings",
            llm_fallback=False,
        )
        self.assertEqual(category, "ai_llm")

    def test_route_category_jailbreak_wins(self):
        category, _ = route_category(
            "Jailbreak the model with a DAN persona and suppress_tokens bypass",
            llm_fallback=False,
        )
        self.assertEqual(category, "ai_llm")

    def test_category_hint_ia_aliased(self):
        category, reason = route_category(
            "Challenge avec un modèle",
            category_hint="securite_ia",
            llm_fallback=False,
        )
        self.assertEqual(category, "ai_llm")
        self.assertIn("hint", reason.lower())


if __name__ == "__main__":
    unittest.main()
