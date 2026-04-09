import shutil
import unittest
from pathlib import Path

from codex_common import BotState, MemoryStore, SessionStore
from tg_codex_bot import TgCodexService


CUSTOM_PERSONA_PROMPT = "CUSTOM PERSONA"


def make_test_root(name: str) -> Path:
    root = Path(__file__).resolve().parent.parent / ".tmp-tests" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


class FakeCodexRunner:
    def run_prompt(self, prompt, cwd, session_id=None, on_update=None, image_paths=None, ephemeral=False):
        return (session_id or "thread-persona", f"answer:{prompt}", "", 0)


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


class TelegramNewThreadPersonaTests(unittest.TestCase):
    def build_service(
        self,
        name: str,
        *,
        enabled: bool = True,
        persona_prompt: str = "",
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
            reply_to_messages=False,
            attach_time_context=True,
            user_display_name="NN",
            new_thread_persona_enabled=enabled,
            new_thread_persona_prompt=persona_prompt,
            memory_auto_enabled=False,
        )

    def test_new_thread_prompt_injects_hidden_persona_once(self) -> None:
        service = self.build_service("new_thread_enabled", persona_prompt=CUSTOM_PERSONA_PROMPT)
        prompt = "当前时间：2026-03-30 21:10 (Asia/Tokyo)\nNN说：晚饭吃晚了"

        wrapped = service._decorate_new_thread_prompt(prompt, active_id=None)

        self.assertIn(CUSTOM_PERSONA_PROMPT, wrapped)
        self.assertIn("下面是NN在这个新线程里发来的第一条消息", wrapped)
        self.assertTrue(wrapped.endswith(prompt))

    def test_existing_thread_prompt_stays_unchanged(self) -> None:
        service = self.build_service("existing_thread")
        prompt = "当前时间：2026-03-30 21:10 (Asia/Tokyo)\nNN说：晚饭吃晚了"

        wrapped = service._decorate_new_thread_prompt(prompt, active_id="thread-123")

        self.assertEqual(wrapped, prompt)

    def test_blank_persona_prompt_keeps_new_thread_prompt_plain(self) -> None:
        service = self.build_service("new_thread_blank")
        prompt = "当前时间：2026-03-30 21:10 (Asia/Tokyo)\nNN说：晚饭吃晚了"

        wrapped = service._decorate_new_thread_prompt(prompt, active_id=None)

        self.assertEqual(wrapped, prompt)

    def test_disabled_persona_keeps_new_thread_prompt_plain(self) -> None:
        service = self.build_service("new_thread_disabled", enabled=False, persona_prompt=CUSTOM_PERSONA_PROMPT)
        prompt = "当前时间：2026-03-30 21:10 (Asia/Tokyo)\nNN说：晚饭吃晚了"

        wrapped = service._decorate_new_thread_prompt(prompt, active_id=None)

        self.assertEqual(wrapped, prompt)

    def test_custom_persona_prompt_can_override_default(self) -> None:
        service = self.build_service("new_thread_custom", persona_prompt=CUSTOM_PERSONA_PROMPT)

        wrapped = service._decorate_new_thread_prompt("hello", active_id=None)

        self.assertIn(CUSTOM_PERSONA_PROMPT, wrapped)


if __name__ == "__main__":
    unittest.main()
