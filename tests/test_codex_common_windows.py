import io
import unittest
from pathlib import Path
from unittest.mock import patch, sentinel

import codex_common
from codex_common import CodexRunner


class FakeStartupInfo:
    def __init__(self) -> None:
        self.dwFlags = 0
        self.wShowWindow = 1


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self.pid = 12345

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class CodexRunnerWindowsTests(unittest.TestCase):
    def test_resolve_codex_bin_returns_configured_value(self) -> None:
        self.assertEqual(codex_common.resolve_codex_bin("custom-codex"), "custom-codex")

    def test_resolve_codex_bin_prefers_codex_from_path(self) -> None:
        with patch("codex_common.shutil.which", return_value=r"C:\tools\codex.exe"):
            self.assertEqual(codex_common.resolve_codex_bin(None), r"C:\tools\codex.exe")

    def test_resolve_codex_bin_falls_back_to_command_name(self) -> None:
        with patch("codex_common.shutil.which", return_value=None):
            self.assertEqual(codex_common.resolve_codex_bin(None), "codex")

    def test_windows_hidden_popen_kwargs_hide_console(self) -> None:
        with patch("codex_common.os.name", "nt"), patch.object(
            codex_common.subprocess,
            "CREATE_NO_WINDOW",
            0x08000000,
            create=True,
        ), patch.object(
            codex_common.subprocess,
            "STARTUPINFO",
            FakeStartupInfo,
            create=True,
        ), patch.object(
            codex_common.subprocess,
            "STARTF_USESHOWWINDOW",
            0x00000001,
            create=True,
        ), patch.object(
            codex_common.subprocess,
            "SW_HIDE",
            0,
            create=True,
        ):
            kwargs = CodexRunner._windows_hidden_popen_kwargs()

        self.assertEqual(kwargs["creationflags"], 0x08000000)
        self.assertIsInstance(kwargs["startupinfo"], FakeStartupInfo)
        self.assertEqual(kwargs["startupinfo"].dwFlags, 0x00000001)
        self.assertEqual(kwargs["startupinfo"].wShowWindow, 0)

    def test_run_prompt_passes_hidden_window_kwargs_to_popen(self) -> None:
        runner = CodexRunner("codex", idle_timeout_sec=0)
        popen_kwargs = {"creationflags": 123, "startupinfo": sentinel.startupinfo}

        with patch.object(
            CodexRunner,
            "_windows_hidden_popen_kwargs",
            return_value=popen_kwargs,
        ), patch("codex_common.subprocess.Popen", return_value=FakeProcess()) as popen_mock:
            runner.run_prompt("hello", Path.cwd())

        self.assertEqual(popen_mock.call_args.kwargs["creationflags"], 123)
        self.assertIs(popen_mock.call_args.kwargs["startupinfo"], sentinel.startupinfo)


if __name__ == "__main__":
    unittest.main()
