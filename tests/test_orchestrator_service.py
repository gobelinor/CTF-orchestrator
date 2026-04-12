import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ctf_orchestrator.orchestrator_service import maybe_write_writeup


class OrchestratorServiceWriteupTest(unittest.TestCase):
    def test_maybe_write_writeup_falls_back_when_worker_writeup_times_out(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            with patch(
                "ctf_orchestrator.orchestrator_service.generate_writeup_markdown",
                side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=300),
            ):
                maybe_write_writeup(
                    workspace=workspace,
                    challenge_name="Solved Challenge",
                    challenge_text="Recover the flag from the noisy trace.",
                    category_hint="misc",
                    target_host=None,
                    final_state={
                        "solved": True,
                        "final_flag": "flag{inline}",
                        "final_summary": "Recovered directly from the trace.",
                        "latest_worker_output": {"commands": []},
                        "history": [],
                    },
                    skills_root=Path("skills"),
                    workers={"codex": object()},
                    backend_sequence=["codex"],
                )

            markdown = (workspace / "writeup.md").read_text(encoding="utf-8")
            self.assertIn("# Writeup", markdown)
            self.assertIn("## Resolution", markdown)
            self.assertIn("flag{inline}", markdown)


if __name__ == "__main__":
    unittest.main()
