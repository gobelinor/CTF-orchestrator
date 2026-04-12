import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ctf_orchestrator.challenges import normalize_challenge_payload
from ctf_orchestrator.cli import _load_env_file, parse_args
from ctf_orchestrator.orchestrator_service import (
    maybe_write_writeup,
    render_writeup_markdown,
    validate_challenge_actionability,
)


class CliNormalizationTest(unittest.TestCase):
    def test_normalizes_target_and_files(self) -> None:
        payload = normalize_challenge_payload(
            {
                "title": "Evaluative",
                "description": "Decode the rogue bot.",
                "category": "misc",
                "ip": "154.57.164.64",
                "port": 31748,
                "files": ["bot.py", "trace.txt"],
                "difficulty": "Very Easy",
                "points": 10,
            }
        )
        self.assertEqual(payload["challenge_name"], "Evaluative")
        self.assertEqual(payload["challenge_text"], "Decode the rogue bot.")
        self.assertEqual(payload["category_hint"], "misc")
        self.assertEqual(payload["target_host"], "154.57.164.64:31748")
        self.assertEqual(payload["artifact_paths"], ["bot.py", "trace.txt"])
        self.assertEqual(payload["challenge_metadata"]["difficulty"], "Very Easy")
        self.assertEqual(payload["challenge_metadata"]["points"], 10)

    def test_load_env_file_sets_defaults_without_overriding_existing_env(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "DISCORD_PARENT_CHANNEL_ID=1480705892918755544",
                        'DISCORD_BOT_TOKEN="secret-token"',
                    ]
                ),
                encoding="utf-8",
            )
            original_token = os.environ.get("DISCORD_BOT_TOKEN")
            try:
                os.environ["DISCORD_BOT_TOKEN"] = "already-set"
                os.environ.pop("DISCORD_PARENT_CHANNEL_ID", None)

                _load_env_file(env_path)

                self.assertEqual(os.environ["DISCORD_BOT_TOKEN"], "already-set")
                self.assertEqual(os.environ["DISCORD_PARENT_CHANNEL_ID"], "1480705892918755544")
            finally:
                if original_token is None:
                    os.environ.pop("DISCORD_BOT_TOKEN", None)
                else:
                    os.environ["DISCORD_BOT_TOKEN"] = original_token
                os.environ.pop("DISCORD_PARENT_CHANNEL_ID", None)

    def test_parse_args_uses_default_dotenv_when_present(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            env_path = root / ".env"
            env_path.write_text("DISCORD_PARENT_CHANNEL_ID=1480705892918755544\n", encoding="utf-8")
            previous_cwd = Path.cwd()
            original_channel = os.environ.get("DISCORD_PARENT_CHANNEL_ID")
            try:
                os.chdir(root)
                os.environ.pop("DISCORD_PARENT_CHANNEL_ID", None)
                args = parse_args(["--challenge-name", "X", "--challenge-text", "Y"])
                self.assertEqual(args.discord_parent_channel_id, "1480705892918755544")
                self.assertEqual(args.env_file.resolve(), env_path.resolve())
            finally:
                os.chdir(previous_cwd)
                if original_channel is None:
                    os.environ.pop("DISCORD_PARENT_CHANNEL_ID", None)
                else:
                    os.environ["DISCORD_PARENT_CHANNEL_ID"] = original_channel

    def test_render_writeup_includes_resolution_and_commands(self) -> None:
        markdown = render_writeup_markdown(
            challenge_name="Bruce Test",
            challenge_text="Recover the password from a structured search problem.",
            category_hint="crypto",
            target_host="socket.example:1234",
            final_state={
                "solved": True,
                "final_flag": "flag{win}",
                "final_summary": "Solved by narrowing the exact search, then validating the recovered password remotely.",
                "latest_worker_output": {
                    "commands": [
                        "python3 solve.py --query",
                        "nc socket.example 1234",
                    ]
                },
                "history": [
                    {
                        "summary": "Built a smaller exact searcher around the surviving candidates.",
                        "evidence": ["The previous broad search was pruned to a small candidate window."],
                        "key_commands": ["python3 scan_exact_frontier.py --n 41 --limit 5"],
                        "inline_scripts": [{"snippet": "from solve import ExactSearcher\nprint('ok')"}],
                    }
                ],
            },
        )

        self.assertIn("# Writeup", markdown)
        self.assertIn("## Resolution", markdown)
        self.assertIn("## Solve", markdown)
        self.assertIn("python3 solve.py --query", markdown)
        self.assertIn("## Scripts", markdown)
        self.assertIn("flag{win}", markdown)

    def test_maybe_write_writeup_only_when_solved(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)

            maybe_write_writeup(
                workspace=workspace,
                challenge_name="Solved Challenge",
                challenge_text="Inline flag challenge.",
                category_hint="misc",
                target_host=None,
                final_state={
                    "solved": True,
                    "final_flag": "flag{inline}",
                    "final_summary": "Recovered directly from the statement.",
                    "latest_worker_output": {"commands": []},
                    "history": [],
                },
            )
            self.assertTrue((workspace / "writeup.md").exists())

        with TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            result = maybe_write_writeup(
                workspace=workspace,
                challenge_name="Unsolved Challenge",
                challenge_text="No flag.",
                category_hint="misc",
                target_host=None,
                final_state={
                    "solved": False,
                    "final_flag": None,
                    "final_summary": "Not solved.",
                    "latest_worker_output": {"commands": []},
                    "history": [],
                },
            )
            self.assertIsNone(result)
            self.assertFalse((workspace / "writeup.md").exists())

    def test_maybe_write_writeup_prefers_worker_generated_markdown(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            with patch(
                "ctf_orchestrator.orchestrator_service.generate_writeup_markdown",
                return_value="# Writeup\n\nSharper than the fallback.\n",
            ) as mocked:
                maybe_write_writeup(
                    workspace=workspace,
                    challenge_name="Solved Challenge",
                    challenge_text="Inline flag challenge.",
                    category_hint="misc",
                    target_host=None,
                    final_state={
                        "solved": True,
                        "final_flag": "flag{inline}",
                        "final_summary": "Recovered directly from the statement.",
                        "latest_worker_output": {"commands": []},
                        "history": [],
                    },
                    skills_root=Path("skills"),
                    workers={"codex": object()},
                    backend_sequence=["codex"],
                )

            self.assertEqual(
                (workspace / "writeup.md").read_text(encoding="utf-8"),
                "# Writeup\n\nSharper than the fallback.\n",
            )
            mocked.assert_called_once()

    def test_maybe_write_writeup_falls_back_when_worker_returns_none(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            with patch("ctf_orchestrator.orchestrator_service.generate_writeup_markdown", return_value=None):
                maybe_write_writeup(
                    workspace=workspace,
                    challenge_name="Solved Challenge",
                    challenge_text="Inline flag challenge.",
                    category_hint="misc",
                    target_host=None,
                    final_state={
                        "solved": True,
                        "final_flag": "flag{inline}",
                        "final_summary": "Recovered directly from the statement.",
                        "latest_worker_output": {"commands": []},
                        "history": [],
                    },
                    skills_root=Path("skills"),
                    workers={"codex": object()},
                    backend_sequence=["codex"],
                )

            markdown = (workspace / "writeup.md").read_text(encoding="utf-8")
            self.assertIn("## Resolution", markdown)
            self.assertIn("## Solve", markdown)

    def test_validate_challenge_actionability_rejects_missing_instance_access(self) -> None:
        with self.assertRaises(SystemExit) as context:
            validate_challenge_actionability(
                "Operating Room",
                None,
                {
                    "import_metadata": {
                        "start_instance_requested": True,
                        "start_instance_result": "failed",
                        "warnings": [
                            "Target host was not detected from the source.",
                            "Container start request timed out before access became available: timed out",
                        ],
                    }
                },
            )

        self.assertIn("not actionable", str(context.exception))
        self.assertIn("start_instance_result=failed", str(context.exception))

    def test_validate_challenge_actionability_allows_target_host_when_present(self) -> None:
        validate_challenge_actionability(
            "Operating Room",
            "espilon.net:35691",
            {
                "import_metadata": {
                    "start_instance_requested": True,
                    "start_instance_result": "reused_current",
                    "warnings": [],
                }
            },
        )


if __name__ == "__main__":
    unittest.main()
