import shutil
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_common import BotState, MemoryStore, SessionStore
from tg_codex_bot import TgCodexService


def make_test_root(name: str) -> Path:
    root = Path(__file__).resolve().parent.parent / ".tmp-tests" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


class FakeCodexRunner:
    def run_prompt(self, prompt, cwd, session_id=None, on_update=None, image_paths=None, ephemeral=False):
        return (session_id or "thread-ctx", f"answer:{prompt}", "", 0)


class FakeTelegramAPI:
    def __init__(self) -> None:
        self.sent = []

    def get_updates(self, offset, timeout=30):
        return []

    def send_message(self, chat_id, text, reply_to=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_to, reply_markup))

    def send_message_with_result(self, chat_id, text, reply_to=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_to, reply_markup))
        return {"message_id": len(self.sent)}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        return None

    def send_chat_action(self, chat_id, action="typing"):
        return None

    def set_my_commands(self, commands):
        return None

    def set_chat_menu_button_commands(self):
        return None


class TelegramTimeContextTests(unittest.TestCase):
    @staticmethod
    def tokyo_ts(hour: int, minute: int) -> int:
        tokyo_tz = timezone(timedelta(hours=9), name="Asia/Tokyo")
        return int(datetime(2026, 3, 30, hour, minute, tzinfo=tokyo_tz).timestamp())

    def build_service(self, name: str) -> TgCodexService:
        root = make_test_root(name)
        return TgCodexService(
            api=FakeTelegramAPI(),
            sessions=SessionStore(root / "sessions"),
            state=BotState(root / "state.json"),
            memory_store=MemoryStore(root / "memory.json"),
            codex=FakeCodexRunner(),
            audio_transcriber=None,
            tts_synthesizer=None,
            default_cwd=root,
            allowed_user_ids={123},
            stream_enabled=False,
            stream_edit_interval_ms=300,
            stream_min_delta_chars=8,
            thinking_status_interval_ms=700,
            reply_to_messages=False,
            attach_time_context=True,
            user_display_name="NN",
            memory_auto_enabled=False,
        )

    def test_text_prompt_includes_tokyo_time_and_name(self) -> None:
        service = self.build_service("text_prompt")

        prompt = service._decorate_text_prompt_with_context("晚上吃什么", self.tokyo_ts(14, 7))

        self.assertEqual(prompt, "当前时间：2026-03-30 14:07 (Asia/Tokyo)\nNN说：晚上吃什么")

    def test_audio_prompt_includes_caption_and_transcript(self) -> None:
        service = self.build_service("audio_prompt")

        prompt = service._decorate_audio_prompt_with_context(
            "我在地铁上，还要晚一点",
            "顺手说一下",
            self.tokyo_ts(9, 30),
        )

        self.assertEqual(
            prompt,
            "当前时间：2026-03-30 09:30 (Asia/Tokyo)\n"
            "NN补充说：顺手说一下\n"
            "NN的语音转写：我在地铁上，还要晚一点",
        )

    def test_handle_chat_message_wraps_prompt_before_run(self) -> None:
        service = self.build_service("chat_message")
        captured = {}

        def fake_run_prompt(chat_id, reply_to, user_id, prompt, image_paths=None, memory_source_text=None):
            captured["chat_id"] = chat_id
            captured["reply_to"] = reply_to
            captured["user_id"] = user_id
            captured["prompt"] = prompt
            captured["memory_source_text"] = memory_source_text

        service._run_prompt = fake_run_prompt  # type: ignore[method-assign]

        service._handle_chat_message(
            chat_id=456,
            reply_to=789,
            user_id=123,
            text="晚上吃什么",
            message_ts=self.tokyo_ts(20, 15),
        )

        self.assertEqual(captured["chat_id"], 456)
        self.assertEqual(captured["reply_to"], 789)
        self.assertEqual(captured["user_id"], 123)
        self.assertEqual(
            captured["prompt"],
            "当前时间：2026-03-30 20:15 (Asia/Tokyo)\nNN说：晚上吃什么",
        )
        self.assertEqual(captured["memory_source_text"], "晚上吃什么")


if __name__ == "__main__":
    unittest.main()
