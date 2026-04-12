import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ctf_orchestrator import import_cli, import_service
from ctf_orchestrator.importers import ImportRequest, ImportedChallenge


def _fake_imported(title: str, category: str = "web", target: str | None = None) -> ImportedChallenge:
    return ImportedChallenge(
        title=title,
        description=f"Description for {title}",
        category=category,
        target_host=target,
        files=[],
        import_metadata={"imported_via_agent": True, "ctfd_challenge_id": 42},
        warnings=[],
    )


class ImportAgentFlowTests(unittest.TestCase):
    def test_load_board_context_dispatches_list_mode(self):
        calls = []

        def fake_agent(*, document, skills_root, selected_challenge, list_mode, start_instance=False, session_cookie=None):
            calls.append({"list_mode": list_mode, "selected": selected_challenge})
            return [_fake_imported("Alpha"), _fake_imported("Beta", category="crypto")]

        with TemporaryDirectory() as tmp:
            source_file = Path(tmp) / "board.txt"
            source_file.write_text("anything", encoding="utf-8")
            import_request = ImportRequest(
                source=str(source_file),
                input_file=None,
                output=None,
                use_stdout=False,
                review=False,
                selected_challenge=None,
                list_only=False,
                session_cookie=None,
                cookie_file=None,
                start_instance=False,
            )
            with patch.object(import_service, "normalize_via_agent", side_effect=fake_agent):
                context = import_service.load_board_context(import_request)

        self.assertEqual(len(context.candidates), 2)
        self.assertEqual(context.candidates[0].title, "Alpha")
        self.assertEqual(calls[0]["list_mode"], True)

    def test_load_board_context_raises_when_agent_returns_empty(self):
        with TemporaryDirectory() as tmp:
            source_file = Path(tmp) / "empty.txt"
            source_file.write_text("x", encoding="utf-8")
            import_request = ImportRequest(
                source=str(source_file),
                input_file=None,
                output=None,
                use_stdout=False,
                review=False,
                selected_challenge=None,
                list_only=False,
                session_cookie=None,
                cookie_file=None,
                start_instance=False,
            )
            with patch.object(import_service, "normalize_via_agent", return_value=[]):
                with self.assertRaises(SystemExit):
                    import_service.load_board_context(import_request)

    def test_import_candidate_uses_single_mode(self):
        def fake_agent(*, document, skills_root, selected_challenge, list_mode, start_instance=False, session_cookie=None):
            if list_mode:
                return [_fake_imported("Alpha")]
            return [_fake_imported(selected_challenge or "Alpha")]

        with TemporaryDirectory() as tmp:
            source_file = Path(tmp) / "board.txt"
            source_file.write_text("anything", encoding="utf-8")
            import_request = ImportRequest(
                source=str(source_file),
                input_file=None,
                output=None,
                use_stdout=False,
                review=False,
                selected_challenge=None,
                list_only=False,
                session_cookie=None,
                cookie_file=None,
                start_instance=False,
            )
            with patch.object(import_service, "normalize_via_agent", side_effect=fake_agent):
                context = import_service.load_board_context(import_request)
                imported = import_service.import_candidate(context, context.candidates[0])

        self.assertEqual(imported.title, "Alpha")
        self.assertTrue(imported.import_metadata["imported_via_agent"])
        self.assertIn("board_source_key", imported.import_metadata)

    def test_import_cli_list_mode_prints_candidates(self):
        def fake_agent(*, document, skills_root, selected_challenge, list_mode, start_instance=False, session_cookie=None):
            return [_fake_imported("Alpha"), _fake_imported("Beta")]

        with TemporaryDirectory() as tmp:
            board_path = Path(tmp) / "board.txt"
            board_path.write_text("x", encoding="utf-8")
            buf = StringIO()
            with patch.object(import_service, "normalize_via_agent", side_effect=fake_agent), \
                 patch("sys.stdout", new=buf):
                code = import_cli.main([str(board_path), "--list"])
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertIn("Alpha", output)
        self.assertIn("Beta", output)

    def test_import_cli_writes_stdout_json_for_single_challenge(self):
        def fake_agent(*, document, skills_root, selected_challenge, list_mode, start_instance=False, session_cookie=None):
            return [_fake_imported("Alpha", category="crypto", target="example.com:1337")]

        with TemporaryDirectory() as tmp:
            board_path = Path(tmp) / "board.txt"
            board_path.write_text("x", encoding="utf-8")
            buf = StringIO()
            with patch.object(import_service, "normalize_via_agent", side_effect=fake_agent), \
                 patch("sys.stdout", new=buf):
                code = import_cli.main([str(board_path), "--stdout"])
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertIn("Alpha", output)
        self.assertIn("crypto", output)
        self.assertIn("example.com:1337", output)


if __name__ == "__main__":
    unittest.main()
