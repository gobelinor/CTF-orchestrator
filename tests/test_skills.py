from pathlib import Path
import unittest

from ctf_orchestrator.skills import load_skills, resolve_specialist_skill, route_category


ROOT = Path(__file__).resolve().parents[1]


class SkillsTest(unittest.TestCase):
    def test_load_skills_finds_ctf_solvers(self) -> None:
        skills = load_skills(ROOT / "skills")
        self.assertIn("ctf-web-solver", skills)
        self.assertIn("ctf-category-router", skills)

    def test_route_category_prefers_hint(self) -> None:
        category, reason = route_category("This sounds like a binary challenge.", "web")
        self.assertEqual(category, "web")
        self.assertIn("explicit category hint", reason)

    def test_resolve_specialist_skill_returns_expected_solver(self) -> None:
        skills = load_skills(ROOT / "skills")
        skill = resolve_specialist_skill("crypto", skills)
        self.assertEqual(skill.slug, "ctf-crypto-solver")


if __name__ == "__main__":
    unittest.main()
