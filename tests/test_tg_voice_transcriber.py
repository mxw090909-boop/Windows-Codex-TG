import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tg_codex_bot import LocalWhisperAudioTranscriber


def make_test_root(name: str) -> Path:
    root = Path(__file__).resolve().parent.parent / ".tmp-tests" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


class DummyModel:
    def transcribe(self, audio, language=None, fp16=False, verbose=False):
        return {"text": "hello from voice"}


class LocalWhisperTranscriberTests(unittest.TestCase):
    def test_transcribe_telegram_audio_closes_and_cleans_temp_file_before_decode(self) -> None:
        root = make_test_root("voice_temp_cleanup")
        seen = {}
        transcriber = LocalWhisperAudioTranscriber(model_name="base")
        real_mkstemp = tempfile.mkstemp

        def fake_decode(file_path: str):
            path = Path(file_path)
            seen["path"] = path
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), b"voice-bytes")
            return [0.1, 0.2]

        with patch("tg_codex_bot.fetch_telegram_audio", return_value=(b"voice-bytes", "sample.ogg", "audio/ogg")), patch.object(
            transcriber,
            "_load_model",
            return_value=DummyModel(),
        ), patch.object(transcriber, "_decode_audio", side_effect=fake_decode), patch(
            "tg_codex_bot.tempfile.mkstemp",
            side_effect=lambda prefix, suffix: real_mkstemp(prefix=prefix, suffix=suffix, dir=root),
        ):
            text = transcriber.transcribe_telegram_audio(
                api=None,
                file_id="voice-1",
                file_name="sample.ogg",
                mime_type="audio/ogg",
                file_size=11,
            )

        self.assertEqual(text, "hello from voice")
        self.assertIn("path", seen)
        self.assertFalse(seen["path"].exists())


if __name__ == "__main__":
    unittest.main()
