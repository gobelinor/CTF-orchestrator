from pathlib import Path
from tempfile import TemporaryDirectory
import io
import unittest
from unittest.mock import patch

from ctf_orchestrator.workspace import prepare_challenge_workspace


class WorkspaceTest(unittest.TestCase):
    def test_prepare_workspace_copies_artifacts_and_manifest(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "source"
            source.mkdir()
            (source / "note.txt").write_text("hello", encoding="utf-8")

            workspace, staged = prepare_challenge_workspace(
                workspace_root=root,
                challenge_name="Evaluative",
                artifact_paths=["note.txt"],
                challenge_payload={"challenge_name": "Evaluative"},
                source_root=source,
            )

            self.assertTrue(workspace.exists())
            self.assertEqual(staged, ["artifacts/note.txt"])
            self.assertEqual((workspace / staged[0]).read_text(encoding="utf-8"), "hello")
            self.assertTrue((workspace / "challenge.json").exists())

    def test_prepare_workspace_downloads_http_artifacts(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)

            with patch("ctf_orchestrator.workspace.request.urlopen", return_value=io.BytesIO(b"print('hi')\n")):
                workspace, staged = prepare_challenge_workspace(
                    workspace_root=root,
                    challenge_name="Hash Stuffing",
                    artifact_paths=["https://cryptohack.org/static/challenges/source.py"],
                    challenge_payload={"challenge_name": "Hash Stuffing"},
                )

            self.assertEqual(staged, ["artifacts/source.py"])
            self.assertEqual((workspace / staged[0]).read_text(encoding="utf-8"), "print('hi')\n")
            self.assertTrue((workspace / "challenge.json").exists())


if __name__ == "__main__":
    unittest.main()
