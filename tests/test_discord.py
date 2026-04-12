from contextlib import redirect_stderr
from io import BytesIO
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from ctf_orchestrator.discord_sync import (
    CampaignDiscordObserver,
    ChallengeDiscordObserver,
    DiscordClient,
    DiscordConfig,
    DiscordDispatcher,
    DiscordHttpTransport,
    DiscordThreadRef,
    resolve_discord_config,
)


class FakeDiscordTransport:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.requests.append((method, path, payload))
        if not self.responses:
            return {}
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeTimer:
    def __init__(self, interval: float, callback) -> None:
        self.interval = interval
        self.callback = callback
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.callback()


class FakeTimerFactory:
    def __init__(self) -> None:
        self.timers: list[FakeTimer] = []

    def __call__(self, interval: float, callback) -> FakeTimer:
        timer = FakeTimer(interval, callback)
        self.timers.append(timer)
        return timer


class FakeHttpResponse:
    def __init__(self, payload: str) -> None:
        self.payload = payload.encode("utf-8")

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class DiscordSyncTest(unittest.TestCase):
    def test_resolve_discord_config_requires_token_and_channel(self) -> None:
        with self.assertRaises(ValueError):
            resolve_discord_config(bot_token="token", parent_channel_id=None)

        config = resolve_discord_config(
            bot_token="token",
            parent_channel_id="123456",
        )
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.parent_channel_id, "123456")

    def test_ensure_challenge_thread_creates_text_thread_and_persists_binding(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "challenge.json").write_text('{"challenge_name":"Discord Test"}', encoding="utf-8")
            client = DiscordClient(
                DiscordConfig(
                    bot_token="token",
                    parent_channel_id="999",
                ),
                transport=FakeDiscordTransport([{"id": "thread-1", "name": "Discord Test"}, {"id": "message-1"}]),
            )

            binding = client.ensure_challenge_thread(
                workspace=workspace,
                challenge_name="Discord Test",
                challenge_text="Investigate the service and recover the flag.",
                category_hint="web",
                target_host="10.10.10.10:31337",
                artifact_paths=["artifacts/note.txt"],
                challenge_metadata={"points": 50},
            )

            self.assertEqual(binding.thread_id, "thread-1")
            request_method, request_path, payload = client.transport.requests[0]
            self.assertEqual((request_method, request_path), ("POST", "/channels/999/threads"))
            self.assertEqual(payload["type"], 11)
            self.assertEqual(client.transport.requests[1][1], "/channels/thread-1/messages")

            manifest = json.loads((workspace / "challenge.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["discord_thread"]["thread_id"], "thread-1")

            same_binding = client.ensure_challenge_thread(
                workspace=workspace,
                challenge_name="Discord Test",
                challenge_text="ignored",
                category_hint=None,
                target_host=None,
            )
            self.assertEqual(same_binding.thread_id, "thread-1")
            self.assertEqual(len(client.transport.requests), 2)

    def test_ensure_challenge_thread_creates_text_thread_then_posts_message(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "challenge.json").write_text('{"challenge_name":"Text Thread"}', encoding="utf-8")
            transport = FakeDiscordTransport(
                [
                    {"id": "thread-2", "name": "Text Thread"},
                    {"id": "message-1"},
                ]
            )
            client = DiscordClient(
                DiscordConfig(
                    bot_token="token",
                    parent_channel_id="321",
                ),
                transport=transport,
            )

            binding = client.ensure_challenge_thread(
                workspace=workspace,
                challenge_name="Text Thread",
                challenge_text="Short description.",
                category_hint=None,
                target_host=None,
            )

            self.assertEqual(binding.thread_id, "thread-2")
            self.assertEqual(transport.requests[0][1], "/channels/321/threads")
            self.assertEqual(transport.requests[0][2]["type"], 11)
            self.assertEqual(transport.requests[1][1], "/channels/thread-2/messages")

    def test_publish_final_posts_writeup_when_solved(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "writeup.md").write_text(
                "# Writeup\n\nShort solve summary.\n\n```bash\npython solve.py\n```",
                encoding="utf-8",
            )
            transport = FakeDiscordTransport([{"id": "message-1"}, {"id": "message-2"}])
            client = DiscordClient(
                DiscordConfig(
                    bot_token="token",
                    parent_channel_id="321",
                ),
                transport=transport,
            )
            thread = DiscordThreadRef(
                thread_id="thread-9",
                thread_name="Thread",
                parent_channel_id="321",
            )

            client.publish_final(
                thread=thread,
                final_state={
                    "solved": True,
                    "stop_reason": "solved",
                    "attempts": 3,
                    "final_summary": "Recovered the password.",
                    "final_flag": "flag{demo}",
                    "workspace": str(workspace),
                },
            )

            self.assertEqual(len(transport.requests), 2)
            self.assertEqual(transport.requests[0][1], "/channels/thread-9/messages")
            self.assertIn("Run completed.", str(transport.requests[0][2]["content"]))
            self.assertIn("# Writeup", str(transport.requests[1][2]["content"]))

    def test_publish_final_skips_writeup_for_unsolved_runs(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "writeup.md").write_text("# Old Writeup", encoding="utf-8")
            transport = FakeDiscordTransport([{"id": "message-1"}])
            client = DiscordClient(
                DiscordConfig(
                    bot_token="token",
                    parent_channel_id="321",
                ),
                transport=transport,
            )
            thread = DiscordThreadRef(
                thread_id="thread-10",
                thread_name="Thread",
                parent_channel_id="321",
            )

            client.publish_final(
                thread=thread,
                final_state={
                    "solved": False,
                    "stop_reason": "max_attempts",
                    "attempts": 4,
                    "final_summary": "No flag.",
                    "workspace": str(workspace),
                },
            )

            self.assertEqual(len(transport.requests), 1)
            self.assertIn("Solved: `no`", str(transport.requests[0][2]["content"]))

    def test_campaign_observer_posts_in_parent_channel_without_creating_thread(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            campaign_dir = Path(tmp_dir)
            stale_binding = campaign_dir / ".discord-campaign-thread.json"
            stale_binding.write_text('{"thread_id":"old","thread_name":"old","parent_channel_id":"321"}', encoding="utf-8")
            transport = FakeDiscordTransport(
                [
                    {"id": "message-1"},
                    {"id": "message-2"},
                    {"id": "message-3"},
                    {"id": "message-4"},
                ]
            )
            client = DiscordClient(
                DiscordConfig(
                    bot_token="token",
                    parent_channel_id="321",
                ),
                transport=transport,
            )
            observer = CampaignDiscordObserver(client, campaign_dir)

            observer.handle_event(
                "campaign_started",
                {
                    "campaign_name": "Campaign challenges",
                    "source_label": "https://ctf.example/challenges",
                    "filters": {"challenge_queries": ["Test"]},
                    "capacities": {"max_parallel_challenges": 2},
                },
            )
            observer.handle_event(
                "campaign_import_completed",
                {"discovered": 3, "eligible": 1, "skipped": 2, "import_failed": 0},
            )
            observer.handle_event(
                "campaign_challenge_started",
                {"challenge_name": "Test", "category": "crypto", "priority_reason": "points=100"},
            )
            observer.handle_event(
                "campaign_completed",
                {"counts": {"solved": 1, "needs_human": 0, "skipped": 2, "import_failed": 0, "interrupted": 0}},
            )

            self.assertFalse(stale_binding.exists())
            self.assertEqual(
                [item[1] for item in transport.requests],
                [
                    "/channels/321/messages",
                    "/channels/321/messages",
                    "/channels/321/messages",
                    "/channels/321/messages",
                ],
            )

    def test_dispatcher_batches_worker_commands_until_flush(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "challenge.json").write_text('{"challenge_name":"Batch Test"}', encoding="utf-8")
            transport = FakeDiscordTransport(
                [
                    {"id": "thread-1", "name": "Batch Test"},
                    {"id": "message-initial"},
                    {"id": "message-batch"},
                ]
            )
            client = DiscordClient(
                DiscordConfig(
                    bot_token="token",
                    parent_channel_id="321",
                ),
                transport=transport,
            )
            timer_factory = FakeTimerFactory()
            dispatcher = DiscordDispatcher(client, flush_interval_seconds=10.0, timer_factory=timer_factory)

            thread = client.ensure_challenge_thread(
                workspace=workspace,
                challenge_name="Batch Test",
                challenge_text="Investigate and recover the flag.",
                category_hint="web",
                target_host="127.0.0.1:31337",
            )
            dispatcher.enqueue_worker_command(
                thread.thread_id,
                status="started",
                backend="codex",
                command="python3 solve.py",
            )
            dispatcher.enqueue_worker_command(
                thread.thread_id,
                status="completed",
                backend="codex",
                command="python3 solve.py",
                exit_code=0,
            )

            self.assertEqual(len(transport.requests), 2)
            self.assertEqual(len(timer_factory.timers), 1)

            timer_factory.timers[0].fire()

            self.assertEqual(len(transport.requests), 3)
            self.assertIn("Worker activity (10s window).", str(transport.requests[2][2]["content"]))
            self.assertIn("[codex] started", str(transport.requests[2][2]["content"]))
            self.assertIn("[codex] completed (exit 0)", str(transport.requests[2][2]["content"]))
            dispatcher.close()

    def test_challenge_observer_flushes_worker_commands_before_final_message(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "challenge.json").write_text('{"challenge_name":"Flush Test"}', encoding="utf-8")
            transport = FakeDiscordTransport(
                [
                    {"id": "thread-9", "name": "Flush Test"},
                    {"id": "message-initial"},
                    {"id": "message-batch"},
                    {"id": "message-final"},
                ]
            )
            client = DiscordClient(
                DiscordConfig(
                    bot_token="token",
                    parent_channel_id="321",
                ),
                transport=transport,
            )
            dispatcher = DiscordDispatcher(client, flush_interval_seconds=10.0, timer_factory=FakeTimerFactory())
            observer = ChallengeDiscordObserver(client, dispatcher)

            observer.handle_event(
                "challenge_workspace_prepared",
                {
                    "workspace": str(workspace),
                    "challenge_name": "Flush Test",
                    "challenge_text": "Recover the flag.",
                    "category_hint": "misc",
                    "target_host": None,
                    "artifact_paths": [],
                    "challenge_metadata": {},
                },
            )
            observer.handle_event(
                "worker_command_started",
                {
                    "workspace": str(workspace),
                    "backend": "codex",
                    "command": "python3 solve.py",
                },
            )
            observer.handle_event(
                "challenge_completed",
                {
                    "workspace": str(workspace),
                    "solved": False,
                    "stop_reason": "max_attempts_reached",
                    "attempts": 1,
                    "final_summary": "No flag.",
                },
            )

            self.assertEqual(len(transport.requests), 4)
            self.assertIn("Worker activity (10s window).", str(transport.requests[2][2]["content"]))
            self.assertIn("Run completed.", str(transport.requests[3][2]["content"]))
            dispatcher.close()

    def test_http_transport_retries_on_429(self) -> None:
        sleeps: list[float] = []
        retry_error = HTTPError(
            url="https://discord.com/api/v10/channels/123/messages",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=BytesIO(b'{"retry_after": 0.25}'),
        )
        responses = [
            retry_error,
            FakeHttpResponse('{"id":"message-1"}'),
        ]

        def fake_urlopen(_request):
            current = responses.pop(0)
            if isinstance(current, Exception):
                raise current
            return current

        transport = DiscordHttpTransport("token", max_retries=1, sleep_fn=sleeps.append)
        with patch("ctf_orchestrator.discord_sync.request.urlopen", side_effect=fake_urlopen):
            payload = transport.request("POST", "/channels/123/messages", payload={"content": "hello"})

        self.assertEqual(payload["id"], "message-1")
        self.assertEqual(sleeps, [0.25])

    def test_dispatcher_requeues_commands_when_flush_temporarily_fails(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "challenge.json").write_text('{"challenge_name":"Retry Batch"}', encoding="utf-8")
            transport = FakeDiscordTransport(
                [
                    {"id": "thread-2", "name": "Retry Batch"},
                    {"id": "message-initial"},
                    RuntimeError("temporary discord failure"),
                    {"id": "message-batch"},
                ]
            )
            client = DiscordClient(
                DiscordConfig(
                    bot_token="token",
                    parent_channel_id="321",
                ),
                transport=transport,
            )
            timer_factory = FakeTimerFactory()
            dispatcher = DiscordDispatcher(client, flush_interval_seconds=10.0, timer_factory=timer_factory)

            thread = client.ensure_challenge_thread(
                workspace=workspace,
                challenge_name="Retry Batch",
                challenge_text="Investigate and recover the flag.",
                category_hint="web",
                target_host="127.0.0.1:31337",
            )
            dispatcher.enqueue_worker_command(
                thread.thread_id,
                status="started",
                backend="codex",
                command="python3 solve.py",
            )

            with redirect_stderr(StringIO()):
                timer_factory.timers[0].fire()
            self.assertEqual(len(transport.requests), 3)

            timer_factory.timers[1].fire()
            self.assertEqual(len(transport.requests), 4)
            self.assertIn("Worker activity (10s window).", str(transport.requests[3][2]["content"]))
            dispatcher.close()


if __name__ == "__main__":
    unittest.main()
