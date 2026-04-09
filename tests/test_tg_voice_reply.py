from __future__ import annotations

import shutil
import threading
import time
import unittest
from pathlib import Path

from codex_common import BotState, MemoryStore, SessionStore
from tg_codex_bot import (
    TTS_CALLBACK_PREFIX,
    TTS_TRIGGER_AUTO,
    TTS_TRIGGER_ECHO,
    TTS_TRIGGER_MANUAL,
    TgCodexService,
)
from tg_tts import SynthesizedVoiceNote


def make_test_root(name: str) -> Path:
    root = Path(__file__).resolve().parent.parent / ".tmp-tests" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


class FakeCodexRunner:
    def run_prompt(self, prompt, cwd, session_id=None, on_update=None, image_paths=None, ephemeral=False):
        return (session_id or "thread-voice", "ok", "", 0)


class FakeTelegramAPI:
    def __init__(self) -> None:
        self.sent = []
        self.voices = []
        self.callback_answers = []
        self.deleted = []
        self.edits = []

    def get_updates(self, offset, timeout=30):
        return []

    def send_message(self, chat_id, text, reply_to=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_to, reply_markup))

    def send_message_with_result(self, chat_id, text, reply_to=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_to, reply_markup))
        return {"message_id": len(self.sent)}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))
        return None

    def send_chat_action(self, chat_id, action="typing"):
        return None

    def set_my_commands(self, commands):
        return None

    def set_chat_menu_button_commands(self):
        return None

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        self.callback_answers.append((callback_query_id, text, show_alert))

    def send_voice_with_result(self, *, chat_id, voice, reply_to=None):
        self.voices.append((chat_id, voice, reply_to))
        return {"message_id": 999}

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return None


class FakeSynthesizer:
    def synthesize_voice_note(self, text: str) -> SynthesizedVoiceNote:
        return SynthesizedVoiceNote(audio_bytes=f"voice:{text}".encode("utf-8"))


class TelegramVoiceReplyTests(unittest.TestCase):
    def build_service(self, root: Path, api: FakeTelegramAPI | None = None) -> TgCodexService:
        return TgCodexService(
            api=api or FakeTelegramAPI(),
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
            tts_backend="minimax",
            tts_mode="auto",
            tts_max_chars=220,
            tts_api_base="https://api.minimaxi.com/v1",
            tts_default_model="speech-2.8-turbo",
            tts_ffmpeg_bin=str(root / "ffmpeg.exe"),
            tts_cache_dir=root / "tts-cache",
        )

    def test_voice_command_saves_key_and_voice_id(self) -> None:
        root = make_test_root("voice_command")
        api = FakeTelegramAPI()
        service = self.build_service(root, api=api)

        service._handle_voice(456, 1, 123, "key sk-1234567890abcdef")
        service._handle_voice(456, 1, 123, "voice male-qn-qingse")
        service._handle_voice(456, 1, 123, "")

        settings = service.state.get_voice_settings(123)
        self.assertEqual(settings["api_key"], "sk-1234567890abcdef")
        self.assertEqual(settings["voice_id"], "male-qn-qingse")
        joined = "\n".join(text for _, text, _, _ in api.sent)
        self.assertIn("sk-123...cdef", joined)
        self.assertIn("male-qn-qingse", joined)
        self.assertIn("默认走文字", joined)
        self.assertNotIn("/voice freq", joined)

    def test_resolve_reply_tts_trigger_prefers_manual_request(self) -> None:
        root = make_test_root("voice_trigger_detect")
        service = self.build_service(root)
        service.state.update_voice_settings(123, api_key="secret-key", voice_id="male-qn-qingse")

        manual = service._resolve_reply_tts_trigger(123, "请直接说一遍给我听", source_kind="text")
        echo = service._resolve_reply_tts_trigger(123, "我刚刚在说话", source_kind="voice")
        auto = service._resolve_reply_tts_trigger(123, "今天有点困", source_kind="text")
        manual_over_echo = service._resolve_reply_tts_trigger(123, "说给我听", source_kind="voice")

        self.assertEqual(manual, TTS_TRIGGER_MANUAL)
        self.assertEqual(echo, TTS_TRIGGER_ECHO)
        self.assertEqual(auto, TTS_TRIGGER_AUTO)
        self.assertEqual(manual_over_echo, TTS_TRIGGER_MANUAL)

    def test_build_reply_delivery_segments_manual_request_prefers_voice(self) -> None:
        root = make_test_root("voice_segments_manual")
        service = self.build_service(root)
        service.state.update_voice_settings(123, api_key="secret-key", voice_id="male-qn-qingse")

        segments = service._build_reply_delivery_segments(
            "好的，已经确认了。请直接语音说一遍。",
            123,
            trigger_hint=TTS_TRIGGER_MANUAL,
        )

        self.assertTrue(segments)
        self.assertTrue(all(is_voice for _, is_voice in segments))

    def test_build_reply_delivery_segments_voice_echo_prefers_voice(self) -> None:
        root = make_test_root("voice_segments_echo")
        service = self.build_service(root)
        service.state.update_voice_settings(123, api_key="secret-key", voice_id="male-qn-qingse")

        segments = service._build_reply_delivery_segments(
            "收到，我刚听完。等我一下，我整理后说给你听。",
            123,
            trigger_hint=TTS_TRIGGER_ECHO,
        )

        self.assertTrue(segments)
        self.assertTrue(any(is_voice for _, is_voice in segments))

    def test_build_reply_delivery_segments_auto_keeps_some_text(self) -> None:
        root = make_test_root("voice_segments_auto")
        service = self.build_service(root)
        service.state.update_voice_settings(123, api_key="secret-key", voice_id="male-qn-qingse")

        segments = service._build_reply_delivery_segments(
            "好的，已经确认了。今天的安排我已经整理好了。",
            123,
            trigger_hint=TTS_TRIGGER_AUTO,
        )

        self.assertEqual(len([1 for _, is_voice in segments if is_voice]), 1)
        self.assertEqual(len([1 for _, is_voice in segments if not is_voice]), 1)

    def test_build_reply_delivery_segments_auto_respects_cooldown(self) -> None:
        root = make_test_root("voice_segments_cooldown")
        service = self.build_service(root)
        service.state.update_voice_settings(123, api_key="secret-key", voice_id="male-qn-qingse")
        service.state.record_voice_reply_result(123, used_voice=True, reason="manual")

        segments = service._build_reply_delivery_segments(
            "好的，已经确认了。请直接语音说一遍。",
            123,
            trigger_hint=TTS_TRIGGER_AUTO,
        )

        self.assertTrue(segments)
        self.assertTrue(all((not is_voice) for _, is_voice in segments))

    def test_build_reply_delivery_segments_keeps_sentence_boundaries(self) -> None:
        root = make_test_root("voice_sentence_boundaries")
        service = self.build_service(root)
        service.state.update_voice_settings(123, api_key="secret-key", voice_id="male-qn-qingse")

        segments = service._build_reply_delivery_segments(
            "好的，已经确认了。C:\\repo\\app.py 在这里。稍后同步结果。",
            123,
            trigger_hint=TTS_TRIGGER_MANUAL,
        )

        texts = [text for text, _ in segments]
        self.assertIn("好的，已经确认了。", texts)
        self.assertIn("C:\\repo\\app.py 在这里。", texts)
        self.assertIn("稍后同步结果。", texts)

    def test_send_delivery_segments_records_reply_result(self) -> None:
        root = make_test_root("voice_reply_history")
        api = FakeTelegramAPI()
        service = self.build_service(root, api=api)
        service.state.update_voice_settings(123, api_key="secret-key", voice_id="male-qn-qingse")
        service._build_user_tts_synthesizer = lambda user_id, request=None: FakeSynthesizer()

        service._send_delivery_segments(
            456,
            88,
            123,
            [("好的，已经确认了。", True), ("现在已经没事了。", False)],
            trigger_hint=TTS_TRIGGER_MANUAL,
        )

        recent = service.state.get_recent_voice_reply_results(123, limit=1)
        self.assertEqual(len(api.voices), 1)
        self.assertEqual(recent[0]["reason"], TTS_TRIGGER_MANUAL)
        self.assertTrue(recent[0]["used_voice"])

    def test_send_delivery_segments_streamed_text_keeps_conversation_bubbles(self) -> None:
        root = make_test_root("voice_stream_text_parts")
        api = FakeTelegramAPI()
        service = self.build_service(root, api=api)
        service.state.update_voice_settings(123, api_key="secret-key", voice_id="male-qn-qingse")

        service._send_delivery_segments(
            456,
            88,
            123,
            [("第一段先发。\n\n第二段补充说明。", False)],
            trigger_hint=TTS_TRIGGER_AUTO,
            stream_message_id=77,
        )

        self.assertEqual(len(api.edits), 1)
        self.assertEqual(api.edits[0][2], "第一段先发。")
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(api.sent[0][1], "第二段补充说明。")
        self.assertIsNone(api.sent[0][2])

    def test_tts_callback_worker_sends_voice(self) -> None:
        root = make_test_root("voice_worker")
        api = FakeTelegramAPI()
        service = self.build_service(root, api=api)
        service.state.update_voice_settings(123, api_key="secret-key", voice_id="male-qn-qingse")
        token = service.state.create_tts_request(123, text="你好呀")
        service._build_user_tts_synthesizer = lambda user_id, request=None: FakeSynthesizer()

        service._run_tts_callback_worker(456, 88, 123, token)

        self.assertEqual(len(api.voices), 1)
        self.assertIsNone(api.voices[0][2])
        self.assertEqual(api.voices[0][1].audio_bytes, "voice:你好呀".encode("utf-8"))

    def test_callback_query_dispatches_tts_token(self) -> None:
        root = make_test_root("voice_callback_query")
        api = FakeTelegramAPI()
        service = self.build_service(root, api=api)
        service.state.update_voice_settings(123, api_key="secret-key", voice_id="male-qn-qingse")
        token = service.state.create_tts_request(123, text="你好呀")
        called = threading.Event()
        captured = []

        def fake_worker(chat_id, reply_to, user_id, passed_token):
            captured.append((chat_id, reply_to, user_id, passed_token))
            called.set()

        service._run_tts_callback_worker = fake_worker
        service._build_user_tts_synthesizer = lambda user_id, request=None: FakeSynthesizer()

        service._handle_callback_query(
            {
                "id": "cq-1",
                "data": f"{TTS_CALLBACK_PREFIX}{token}",
                "message": {"chat": {"id": 456}, "message_id": 77},
                "from": {"id": 123},
            }
        )

        self.assertTrue(called.wait(1))
        self.assertEqual(captured, [(456, 77, 123, token)])
        self.assertEqual(api.callback_answers[0][1], "正在生成语音。")


if __name__ == "__main__":
    unittest.main()
