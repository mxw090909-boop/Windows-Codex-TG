from __future__ import annotations

import shutil
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_common import BotState, MemoryStore, SessionStore
from tg_codex_bot import TgCodexService


CUSTOM_HEARTBEAT_PROMPT = "CUSTOM HEARTBEAT"
CUSTOM_BANNED_PATTERNS = ["在吗"]


def make_test_root(name: str) -> Path:
    root = Path(__file__).resolve().parent.parent / ".tmp-tests" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


class FakeCodexRunner:
    def run_prompt(self, prompt, cwd, session_id=None, on_update=None, image_paths=None, ephemeral=False):
        return (session_id or "heartbeat-prompt", f"answer:{prompt}", "", 0)


class FakeTelegramAPI:
    def get_updates(self, offset, timeout=30):
        return []

    def send_message(self, chat_id, text, reply_to=None, reply_markup=None):
        return None

    def send_message_with_result(self, chat_id, text, reply_to=None, reply_markup=None):
        return {"message_id": 1}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        return None

    def send_chat_action(self, chat_id, action="typing"):
        return None

    def set_my_commands(self, commands):
        return None

    def set_chat_menu_button_commands(self):
        return None


class TelegramHeartbeatPromptTests(unittest.TestCase):
    @staticmethod
    def tokyo_ts(hour: int, minute: int = 0) -> int:
        tokyo_tz = timezone(timedelta(hours=9), name="Asia/Tokyo")
        return int(datetime(2026, 3, 30, hour, minute, tzinfo=tokyo_tz).timestamp())

    def build_service(
        self,
        name: str,
        *,
        heartbeat_prompt: str = "",
        heartbeat_banned_patterns: list[str] | None = None,
        heartbeat_templates: list[str] | None = None,
    ) -> TgCodexService:
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
            heartbeat_session_prompt=heartbeat_prompt,
            heartbeat_banned_patterns=heartbeat_banned_patterns,
            heartbeat_template_messages=heartbeat_templates,
            memory_auto_enabled=False,
        )

    def test_first_heartbeat_prompt_mentions_context_and_time(self) -> None:
        service = self.build_service(
            "heartbeat_first_prompt",
            heartbeat_prompt=CUSTOM_HEARTBEAT_PROMPT,
            heartbeat_banned_patterns=CUSTOM_BANNED_PATTERNS,
        )

        prompt = service._build_heartbeat_prompt(
            now_ts=self.tokyo_ts(16, 30),
            heartbeat={"unanswered_count": 0},
            last_user_at=self.tokyo_ts(15, 45),
        )

        self.assertIn(CUSTOM_HEARTBEAT_PROMPT, prompt)
        self.assertIn("当前东京时间：2026-03-30 16:30", prompt)
        self.assertIn("距离用户上次发消息约：45 分钟", prompt)
        self.assertIn(f"“{CUSTOM_BANNED_PATTERNS[0]}”", prompt)
        self.assertIn("请顺着当前会话内容和这个时间点，自然地来碰她一下。", prompt)
        self.assertNotIn("SKIP", prompt)

    def test_followup_heartbeat_prompt_can_choose_skip(self) -> None:
        service = self.build_service("heartbeat_followup_prompt", heartbeat_prompt=CUSTOM_HEARTBEAT_PROMPT)

        prompt = service._build_heartbeat_prompt(
            now_ts=self.tokyo_ts(18, 0),
            heartbeat={
                "unanswered_count": 2,
                "last_heartbeat_at": self.tokyo_ts(17, 20),
            },
            last_user_at=self.tokyo_ts(16, 30),
        )

        self.assertIn("如果你判断现在不该继续发，只输出：SKIP", prompt)
        self.assertIn("连续未回复的主动消息次数：2", prompt)
        self.assertIn("距离上次主动消息约：40 分钟", prompt)

    def test_force_heartbeat_prompt_never_asks_for_skip(self) -> None:
        service = self.build_service("heartbeat_force_prompt", heartbeat_prompt=CUSTOM_HEARTBEAT_PROMPT)

        prompt = service._build_heartbeat_prompt(
            now_ts=self.tokyo_ts(21, 10),
            heartbeat={"unanswered_count": 3},
            last_user_at=self.tokyo_ts(20, 50),
            force=True,
        )

        self.assertIn("这是用户刚刚手动触发的一次主动消息请求，直接发消息，不要输出 SKIP。", prompt)
        self.assertNotIn("如果你判断现在不该继续发，只输出：SKIP", prompt)

    def test_blank_heartbeat_prompt_returns_empty(self) -> None:
        service = self.build_service("heartbeat_blank_prompt")

        prompt = service._build_heartbeat_prompt(
            now_ts=self.tokyo_ts(12, 0),
            heartbeat={"unanswered_count": 0},
            last_user_at=self.tokyo_ts(11, 0),
        )

        self.assertEqual(prompt, "")

    def test_custom_heartbeat_prompt_override_is_used(self) -> None:
        service = self.build_service("heartbeat_custom_prompt", heartbeat_prompt=CUSTOM_HEARTBEAT_PROMPT)

        prompt = service._build_heartbeat_prompt(
            now_ts=self.tokyo_ts(12, 0),
            heartbeat={"unanswered_count": 0},
            last_user_at=self.tokyo_ts(11, 0),
        )

        self.assertIn(CUSTOM_HEARTBEAT_PROMPT, prompt)


if __name__ == "__main__":
    unittest.main()
