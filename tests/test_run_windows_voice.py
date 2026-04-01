import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import run_windows


class RunWindowsVoiceTests(unittest.TestCase):
    def test_auto_enables_local_voice_when_environment_is_ready(self) -> None:
        config = {
            "TG_VOICE_TRANSCRIBE_ENABLED": "",
            "TG_VOICE_TRANSCRIBE_BACKEND": "local-whisper",
        }
        output = io.StringIO()
        with redirect_stdout(output), patch("run_windows.env_value", return_value=""), patch(
            "run_windows.probe_tg_local_voice_env",
            return_value=(True, True),
        ):
            updated = run_windows.configure_tg_voice_defaults(dict(config))

        self.assertEqual(updated["TG_VOICE_TRANSCRIBE_ENABLED"], "1")
        self.assertEqual(updated["TG_VOICE_TRANSCRIBE_BACKEND"], "local-whisper")

    def test_auto_disables_local_voice_when_environment_is_missing(self) -> None:
        config = {
            "TG_VOICE_TRANSCRIBE_ENABLED": "",
            "TG_VOICE_TRANSCRIBE_BACKEND": "local-whisper",
        }
        output = io.StringIO()
        with redirect_stdout(output), patch("run_windows.env_value", return_value=""), patch(
            "run_windows.probe_tg_local_voice_env",
            return_value=(False, False),
        ):
            updated = run_windows.configure_tg_voice_defaults(dict(config))

        self.assertEqual(updated["TG_VOICE_TRANSCRIBE_ENABLED"], "0")
        self.assertIn("缺少 whisper/torch 依赖", output.getvalue())
        self.assertIn("缺少 ffmpeg", output.getvalue())

    def test_explicit_enable_is_preserved(self) -> None:
        config = {
            "TG_VOICE_TRANSCRIBE_ENABLED": "1",
            "TG_VOICE_TRANSCRIBE_BACKEND": "local-whisper",
        }
        with patch("run_windows.env_value", return_value="1"), patch(
            "run_windows.probe_tg_local_voice_env",
            return_value=(True, True),
        ):
            updated = run_windows.configure_tg_voice_defaults(dict(config))

        self.assertEqual(updated["TG_VOICE_TRANSCRIBE_ENABLED"], "1")


if __name__ == "__main__":
    unittest.main()
