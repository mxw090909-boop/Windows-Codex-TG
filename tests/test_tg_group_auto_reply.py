from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from codex_common import BotState, MemoryStore, SessionStore
from tg_codex_bot import TgCodexService


def make_test_root(name: str) -> Path:
    root = Path(__file__).resolve().parent.parent / ".tmp-tests" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


class FakeCodexRunner:
    def __init__(self, answers: list[str] | None = None) -> None:
        self.answers = list(answers or [])
        self.calls = []

    def run_prompt(self, prompt, cwd, session_id=None, on_update=None, image_paths=None, ephemeral=False):
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": Path(cwd),
                "session_id": session_id,
                "ephemeral": bool(ephemeral),
            }
        )
        answer = self.answers.pop(0) if self.answers else '{"action":"skip"}'
        return (session_id or "thread-group", answer, "", 0)


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


class TelegramGroupAutoReplyTests(unittest.TestCase):
    def build_service(
        self,
        root: Path,
        *,
        api: FakeTelegramAPI | None = None,
        runner: FakeCodexRunner | None = None,
    ) -> TgCodexService:
        return TgCodexService(
            api=api or FakeTelegramAPI(),
            sessions=SessionStore(root / "sessions"),
            state=BotState(root / "state.json"),
            memory_store=MemoryStore(root / "memory.json"),
            codex=runner or FakeCodexRunner(),
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
            user_display_name="Friend",
            memory_auto_enabled=False,
            group_auto_reply_chat_ids={-1001},
            group_auto_reply_prompt="GROUP PROMPT",
        )

    def test_non_allowlisted_group_member_is_silently_skipped_when_gate_says_skip(self) -> None:
        root = make_test_root("group_auto_reply_skip")
        api = FakeTelegramAPI()
        runner = FakeCodexRunner(['{"action":"skip"}'])
        service = self.build_service(root, api=api, runner=runner)

        service._handle_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 77,
                    "date": 1710000000,
                    "chat": {"id": -1001, "type": "supergroup"},
                    "from": {"id": 999, "first_name": "Alice"},
                    "text": "只是群友闲聊",
                },
            }
        )

        self.assertEqual(api.sent, [])
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(runner.calls[0]["ephemeral"])

    def test_non_allowlisted_group_member_uses_group_actor_when_gate_sends(self) -> None:
        root = make_test_root("group_auto_reply_send")
        runner = FakeCodexRunner(['{"action":"send"}'])
        service = self.build_service(root, runner=runner)
        captured = {}

        def fake_run_prompt(
            chat_id,
            reply_to,
            user_id,
            prompt,
            image_paths=None,
            memory_source_text=None,
            voice_trigger_hint=None,
        ):
            captured["chat_id"] = chat_id
            captured["reply_to"] = reply_to
            captured["user_id"] = user_id
            captured["prompt"] = prompt
            captured["memory_source_text"] = memory_source_text
            captured["voice_trigger_hint"] = voice_trigger_hint

        service._run_prompt = fake_run_prompt  # type: ignore[method-assign]

        service._handle_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 88,
                    "date": 1710000000,
                    "chat": {"id": -1001, "type": "supergroup"},
                    "from": {"id": 999, "first_name": "Alice"},
                    "text": "@bot 你在吗",
                },
            }
        )

        self.assertEqual(captured["chat_id"], -1001)
        self.assertEqual(captured["reply_to"], 88)
        self.assertEqual(captured["user_id"], "group:-1001")
        self.assertEqual(captured["memory_source_text"], "@bot 你在吗")
        self.assertEqual(captured["voice_trigger_hint"], "none")
        self.assertIn("只在群里自然接话", captured["prompt"])
        self.assertIn("GROUP PROMPT", captured["prompt"])
        self.assertEqual(service.state.get_active(123), (None, None))

    def test_non_allowlisted_group_command_is_ignored(self) -> None:
        root = make_test_root("group_auto_reply_command")
        api = FakeTelegramAPI()
        runner = FakeCodexRunner()
        service = self.build_service(root, api=api, runner=runner)

        service._handle_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 90,
                    "date": 1710000000,
                    "chat": {"id": -1001, "type": "supergroup"},
                    "from": {"id": 999, "first_name": "Alice"},
                    "text": "/sessions",
                },
            }
        )

        self.assertEqual(api.sent, [])
        self.assertEqual(runner.calls, [])

    def test_private_non_allowlisted_user_still_gets_denied(self) -> None:
        root = make_test_root("group_auto_reply_private_still_denied")
        api = FakeTelegramAPI()
        service = self.build_service(root, api=api)

        service._handle_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 91,
                    "date": 1710000000,
                    "chat": {"id": 555, "type": "private"},
                    "from": {"id": 999, "first_name": "Alice"},
                    "text": "hi",
                },
            }
        )

        self.assertEqual(len(api.sent), 1)
        self.assertIn("没有权限", api.sent[0][1])


if __name__ == "__main__":
    unittest.main()
