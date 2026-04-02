from __future__ import annotations

import shutil
import threading
import time
import unittest
from pathlib import Path

from codex_common import BotState, MemoryStore, SessionStore
from tg_codex_bot import TTS_CALLBACK_PREFIX, TgCodexService
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

    def test_voice_command_saves_key_voice_id_and_frequency(self) -> None:
        root = make_test_root("voice_command")
        api = FakeTelegramAPI()
        service = self.build_service(root, api=api)

        service._handle_voice(456, 1, 123, "key sk-1234567890abcdef")
        service._handle_voice(456, 1, 123, "voice male-qn-qingse")
        service._handle_voice(456, 1, 123, "freq high")
        service._handle_voice(456, 1, 123, "")

        settings = service.state.get_voice_settings(123)
        self.assertEqual(settings["api_key"], "sk-1234567890abcdef")
        self.assertEqual(settings["voice_id"], "male-qn-qingse")
        self.assertEqual(settings["frequency"], "high")
        joined = "\n".join(text for _, text, _, _ in api.sent)
        self.assertIn("sk-123...cdef", joined)
        self.assertIn("male-qn-qingse", joined)
        self.assertIn("频率: 高", joined)

    def test_offer_conversation_voice_returns_inline_button_when_ready(self) -> None:
        root = make_test_root("voice_button")
        service = self.build_service(root)
        service.state.update_voice_settings(123, api_key="secret-key", voice_id="male-qn-qingse")

        markup = service._maybe_offer_conversation_voice(456, "我现在就陪你说。", user_id=123)

        self.assertIsNotNone(markup)
        callback_data = markup["inline_keyboard"][0][0]["callback_data"]
        self.assertTrue(callback_data.startswith(TTS_CALLBACK_PREFIX))
        token = callback_data[len(TTS_CALLBACK_PREFIX) :]
        request = service.state.get_tts_request(123, token)
        self.assertIsNotNone(request)
        self.assertEqual(request["text"], "我现在就陪你说。")

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
        self.assertEqual(api.voices[0][1].audio_bytes, b"voice:\xe4\xbd\xa0\xe5\xa5\xbd\xe5\x91\x80")

    def test_queue_conversation_voice_reply_sends_voice_automatically(self) -> None:
        root = make_test_root("voice_auto_send")
        api = FakeTelegramAPI()
        service = self.build_service(root, api=api)
        service.state.update_voice_settings(123, api_key="secret-key", voice_id="male-qn-qingse")
        service._build_user_tts_synthesizer = lambda user_id, request=None: FakeSynthesizer()

        service._queue_conversation_voice_reply(456, "你好呀", reply_to=88, user_id=123)

        deadline = time.time() + 1.0
        while time.time() < deadline and not api.voices:
            time.sleep(0.01)

        self.assertEqual(len(api.voices), 1)
        self.assertIsNone(api.voices[0][2])
        self.assertEqual(api.voices[0][1].audio_bytes, b"voice:\xe4\xbd\xa0\xe5\xa5\xbd\xe5\x91\x80")

    def test_build_reply_delivery_segments_can_mix_voice_and_text(self) -> None:
        root = make_test_root("voice_segments_mix")
        service = self.build_service(root)
        service.state.update_voice_settings(
            123,
            api_key="secret-key",
            voice_id="male-qn-qingse",
            frequency="high",
        )

        segments = service._build_reply_delivery_segments(
            "过来让我抱一下。C:\\repo\\app.py 在这里。别怕，我在。",
            123,
        )

        self.assertGreaterEqual(len(segments), 2)
        self.assertTrue(any(is_voice for _, is_voice in segments))
        self.assertTrue(any((not is_voice) for _, is_voice in segments))

    def test_build_reply_delivery_segments_keeps_sentence_boundaries(self) -> None:
        root = make_test_root("voice_sentence_boundaries")
        service = self.build_service(root)
        service.state.update_voice_settings(
            123,
            api_key="secret-key",
            voice_id="male-qn-qingse",
            frequency="high",
        )

        segments = service._build_reply_delivery_segments(
            "过来让我抱一下，别怕，我在。C:\\repo\\app.py 在这里。晚点我再陪你。",
            123,
        )

        texts = [text for text, _ in segments]
        self.assertIn("过来让我抱一下，别怕，我在。", texts)
        self.assertIn("C:\\repo\\app.py 在这里。", texts)
        self.assertIn("晚点我再陪你。", texts)

    def test_low_frequency_limits_voice_segments(self) -> None:
        root = make_test_root("voice_segments_low")
        service = self.build_service(root)
        service.state.update_voice_settings(
            123,
            api_key="secret-key",
            voice_id="male-qn-qingse",
            frequency="low",
        )

        segments = service._build_reply_delivery_segments(
            "过来让我抱一下。C:\\repo\\app.py 在这里。别怕，我在。",
            123,
        )

        voice_segments = [text for text, is_voice in segments if is_voice]
        self.assertEqual(len(voice_segments), 1)

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
        self.assertEqual(api.callback_answers[0][1], "好，我现在开口。")


if __name__ == "__main__":
    unittest.main()
