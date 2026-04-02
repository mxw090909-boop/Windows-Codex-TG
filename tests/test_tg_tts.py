import shutil
import unittest
from pathlib import Path

from tg_tts import (
    DEFAULT_MINIMAX_MODEL,
    LocalGptSovitsTtsSynthesizer,
    MiniMaxTtsSynthesizer,
    derive_prompt_text_from_reference,
    is_tts_reply_candidate,
)


def make_test_root(name: str) -> Path:
    root = Path(__file__).resolve().parent.parent / ".tmp-tests" / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


class TelegramTtsHelpersTests(unittest.TestCase):
    def test_derive_prompt_text_from_reference_filename(self) -> None:
        text = derive_prompt_text_from_reference(
            r"C:\COVE\Cove_GSV\reference_audios\中文\emotions\【默认】还有编写和调试计算机程序的能力。.wav"
        )
        self.assertEqual(text, "还有编写和调试计算机程序的能力。")

    def test_auto_mode_skips_code_like_replies(self) -> None:
        self.assertFalse(is_tts_reply_candidate("```python\nprint('hi')\n```", mode="auto", max_chars=220))
        self.assertFalse(is_tts_reply_candidate("文件在 C:\\repo\\app.py", mode="auto", max_chars=220))

    def test_auto_mode_accepts_short_spoken_reply(self) -> None:
        self.assertTrue(is_tts_reply_candidate("好的，已经处理好了。", mode="auto", max_chars=220))

    def test_relative_paths_resolve_against_root(self) -> None:
        root = make_test_root("tts_relative_paths")
        (root / "api_v2.py").write_text("# stub\n", encoding="utf-8")
        (root / "venv" / "Scripts").mkdir(parents=True, exist_ok=True)
        (root / "venv" / "Scripts" / "python.exe").write_bytes(b"")
        (root / "TEMP").mkdir(parents=True, exist_ok=True)
        (root / "ffmpeg.exe").write_bytes(b"")
        (root / "GPT_weights").mkdir(parents=True, exist_ok=True)
        (root / "SoVITS_weights").mkdir(parents=True, exist_ok=True)
        (root / "reference_audios").mkdir(parents=True, exist_ok=True)
        ref_audio = root / "reference_audios" / "【默认】系统提示.wav"
        ref_audio.write_bytes(b"wav")
        gpt_weights = root / "GPT_weights" / "Cove.ckpt"
        gpt_weights.write_bytes(b"gpt")
        sovits_weights = root / "SoVITS_weights" / "Cove.pth"
        sovits_weights.write_bytes(b"sovits")

        synth = LocalGptSovitsTtsSynthesizer(
            root_dir=str(root),
            ref_audio_path="reference_audios/【默认】系统提示.wav",
            gpt_weights_path="GPT_weights/Cove.ckpt",
            sovits_weights_path="SoVITS_weights/Cove.pth",
            ffmpeg_bin=str(root / "ffmpeg.exe"),
        )

        self.assertEqual(synth.ref_audio_path, ref_audio.resolve())
        self.assertEqual(synth.gpt_weights_path, gpt_weights.resolve())
        self.assertEqual(synth.sovits_weights_path, sovits_weights.resolve())
        self.assertEqual(synth.prompt_text, "系统提示")

    def test_minimax_cache_path_uses_voice_and_model(self) -> None:
        root = make_test_root("tts_minimax_cache")
        ffmpeg_bin = root / "ffmpeg.exe"
        ffmpeg_bin.write_bytes(b"")
        synth = MiniMaxTtsSynthesizer(
            api_key="secret",
            voice_id="male-qn-qingse",
            ffmpeg_bin=str(ffmpeg_bin),
            cache_dir=root / "cache",
        )
        first = synth._cache_path("处理完成")
        second = synth._cache_path("处理完成")
        third = MiniMaxTtsSynthesizer(
            api_key="secret",
            voice_id="female-yujie",
            ffmpeg_bin=str(ffmpeg_bin),
            cache_dir=root / "cache",
            model=DEFAULT_MINIMAX_MODEL,
        )._cache_path("处理完成")

        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)


if __name__ == "__main__":
    unittest.main()
