import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_common import BotState, MemoryStore, SessionStore
from tg_codex_bot import HEARTBEAT_SESSION_PROMPT, TgCodexService


def write_session_file(root: Path, session_id: str, cwd: str, title_prompt: str) -> None:
    day_dir = root / "2026" / "03" / "30"
    day_dir.mkdir(parents=True, exist_ok=True)
    payloads = [
        {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": "2026-03-30T00:00:00Z",
                "cwd": cwd,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": title_prompt,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "done",
            },
        },
    ]
    target = day_dir / f"{session_id}.jsonl"
    target.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in payloads), encoding="utf-8")


class FakeCodexRunner:
    def __init__(self) -> None:
        self.calls = []
        self.decision_answer = "followup-answer"

    def run_prompt(self, prompt, cwd, session_id=None, on_update=None, image_paths=None, ephemeral=False):
        self.calls.append((prompt, str(cwd), session_id))
        if HEARTBEAT_SESSION_PROMPT in prompt and "只输出：SKIP" not in prompt:
            answer = "heartbeat-answer"
        elif "只输出：SKIP" in prompt:
            answer = self.decision_answer
        else:
            answer = f"answer:{prompt}"
        return (session_id or "thread-123", answer, "", 0)


class RecordingTelegramAPI:
    def __init__(self) -> None:
        self.sent = []
        self.edits = []

    def get_updates(self, offset, timeout=30):
        return []

    def send_message(self, chat_id, text, reply_to=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_to, reply_markup))

    def send_message_with_result(self, chat_id, text, reply_to=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_to, reply_markup))
        return {"message_id": len(self.sent)}

    def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    def send_chat_action(self, chat_id, action="typing"):
        return None

    def set_my_commands(self, commands):
        return None

    def set_chat_menu_button_commands(self):
        return None


class TelegramHeartbeatTests(unittest.TestCase):
    @staticmethod
    def tokyo_ts(hour: int, minute: int = 0) -> int:
        tokyo_tz = timezone(timedelta(hours=9), name="Asia/Tokyo")
        return int(datetime(2026, 3, 30, hour, minute, tzinfo=tokyo_tz).timestamp())

    def build_service(self, root: Path, api: RecordingTelegramAPI, runner: FakeCodexRunner) -> TgCodexService:
        return TgCodexService(
            api=api,
            sessions=SessionStore(root / "sessions"),
            state=BotState(root / "state.json"),
            memory_store=MemoryStore(root / "memory.json"),
            codex=runner,
            audio_transcriber=None,
            tts_synthesizer=None,
            default_cwd=root,
            allowed_user_ids={123},
            stream_enabled=False,
            stream_edit_interval_ms=300,
            stream_min_delta_chars=8,
            thinking_status_interval_ms=700,
            memory_auto_enabled=False,
        )

    def test_heartbeat_command_enables_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            api = RecordingTelegramAPI()
            runner = FakeCodexRunner()
            service = self.build_service(root, api, runner)

            service._handle_heartbeat(chat_id=456, reply_to=1, user_id=123, arg="on 5")

            heartbeat = service.state.get_heartbeat(123)
            self.assertTrue(heartbeat.get("enabled"))
            self.assertEqual(heartbeat.get("interval_sec"), 300)
            self.assertTrue(any("心跳模式开了" in text for _, text, _, _ in api.sent))

    def test_due_heartbeat_respects_tokyo_daytime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            api = RecordingTelegramAPI()
            runner = FakeCodexRunner()
            service = self.build_service(root, api, runner)
            service.state.touch_user(123, 456, at=self.tokyo_ts(6, 0))
            service.state.configure_heartbeat(123, enabled=True, interval_sec=60)

            service._run_due_heartbeats_once(now_ts=self.tokyo_ts(7, 59))
            self.assertEqual(api.sent, [])

    def test_due_heartbeat_sends_template_without_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            api = RecordingTelegramAPI()
            runner = FakeCodexRunner()
            service = self.build_service(root, api, runner)
            service.state.touch_user(123, 456, at=100)
            service.state.configure_heartbeat(123, enabled=True, interval_sec=60)

            service._run_due_heartbeats_once(now_ts=699)
            self.assertEqual(api.sent, [])

            service._run_due_heartbeats_once(now_ts=700)
            self.assertEqual(len(api.sent), 1)
            self.assertIsNone(api.sent[0][2])
            self.assertTrue(service.state.get_heartbeat(123).get("last_heartbeat_at"))
            self.assertEqual(runner.calls, [])

    def test_due_heartbeat_stays_silent_if_recent_bot_interaction_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            api = RecordingTelegramAPI()
            runner = FakeCodexRunner()
            service = self.build_service(root, api, runner)
            service.state.touch_user(123, 456, at=100)
            service.state.touch_assistant(123, 456, at=650)
            service.state.configure_heartbeat(123, enabled=True, interval_sec=60)

            service._run_due_heartbeats_once(now_ts=1000)
            self.assertEqual(api.sent, [])

    def test_conversation_parts_split_long_prose(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            api = RecordingTelegramAPI()
            runner = FakeCodexRunner()
            service = self.build_service(root, api, runner)
            text = "你先别急，我慢慢跟你说。" * 40

            parts = service._conversation_parts(text)

            self.assertGreater(len(parts), 1)
            self.assertTrue(all(len(part) <= 180 for part in parts))

    def test_due_heartbeat_reuses_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            write_session_file(root / "sessions", "sess-1", str(root), "hello")
            api = RecordingTelegramAPI()
            runner = FakeCodexRunner()
            service = self.build_service(root, api, runner)
            service.state.set_active_session(123, "sess-1", str(root))
            service.state.set_heartbeat_context(123, "sess-1", str(root))
            service.state.touch_user(123, 456, at=100)
            service.state.configure_heartbeat(123, enabled=True, interval_sec=60)

            service._run_due_heartbeats_once(now_ts=700)

            deadline = time.time() + 2
            while time.time() < deadline:
                if any("heartbeat-answer" in text for _, text, _, _ in api.sent):
                    break
                time.sleep(0.05)

            self.assertEqual(len(runner.calls), 1)
            prompt, cwd, session_id = runner.calls[0]
            self.assertIn(HEARTBEAT_SESSION_PROMPT, prompt)
            self.assertIn("请顺着当前会话内容和这个时间点，自然地来碰她一下。", prompt)
            self.assertEqual(cwd, str(root))
            self.assertEqual(session_id, "sess-1")
            self.assertTrue(any("heartbeat-answer" in text for _, text, _, _ in api.sent))

    def test_unanswered_heartbeat_can_choose_to_skip_followup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            write_session_file(root / "sessions", "sess-1", str(root), "hello")
            api = RecordingTelegramAPI()
            runner = FakeCodexRunner()
            runner.decision_answer = "SKIP"
            service = self.build_service(root, api, runner)
            service.state.set_heartbeat_context(123, "sess-1", str(root))
            service.state.touch_user(123, 456, at=100)
            service.state.mark_heartbeat_sent(123, 456, at=100, session_id="sess-1", cwd=str(root))
            service.state.configure_heartbeat(123, enabled=True, interval_sec=60)

            service._run_due_heartbeats_once(now_ts=700)
            time.sleep(0.1)

            self.assertEqual(api.sent, [])
            self.assertEqual(len(runner.calls), 1)
            prompt, _, _ = runner.calls[0]
            self.assertIn("只输出：SKIP", prompt)
            self.assertEqual(service.state.get_heartbeat(123).get("unanswered_count"), 1)

    def test_unanswered_heartbeat_can_send_followup_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            write_session_file(root / "sessions", "sess-1", str(root), "hello")
            api = RecordingTelegramAPI()
            runner = FakeCodexRunner()
            runner.decision_answer = "你怎么还没理我，今天在忙什么？"
            service = self.build_service(root, api, runner)
            service.state.set_heartbeat_context(123, "sess-1", str(root))
            service.state.touch_user(123, 456, at=100)
            service.state.mark_heartbeat_sent(123, 456, at=100, session_id="sess-1", cwd=str(root))
            service.state.configure_heartbeat(123, enabled=True, interval_sec=60)

            service._run_due_heartbeats_once(now_ts=700)

            deadline = time.time() + 2
            while time.time() < deadline:
                if any("今天在忙什么" in text for _, text, _, _ in api.sent):
                    break
                time.sleep(0.05)

            self.assertTrue(any("今天在忙什么" in text for _, text, _, _ in api.sent))
            self.assertEqual(service.state.get_heartbeat(123).get("unanswered_count"), 2)


    def test_conversation_parts_keep_code_block_as_separate_bubble(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            api = RecordingTelegramAPI()
            runner = FakeCodexRunner()
            service = self.build_service(root, api, runner)
            text = (
                "前面先说几句铺垫，让这一段足够长，应该被拆成单独的聊天气泡。" * 4
                + "\n\n```python\nprint('hello')\nprint('world')\n```\n\n"
                + "后面再补两句收尾，也应该继续作为普通聊天内容单独发出去。" * 3
            )

            parts = service._conversation_parts(text)

            self.assertGreaterEqual(len(parts), 3)
            self.assertTrue(any("```python" in part for part in parts))
            self.assertTrue(any("后面再补两句收尾" in part for part in parts))

    def test_send_message_does_not_quote_user_message_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            api = RecordingTelegramAPI()
            runner = FakeCodexRunner()
            service = self.build_service(root, api, runner)

            service._send_message(chat_id=456, text="hello", reply_to=99, user_id=123)

            self.assertEqual(len(api.sent), 1)
            self.assertIsNone(api.sent[0][2])


if __name__ == "__main__":
    unittest.main()
