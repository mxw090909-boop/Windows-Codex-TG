import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run_windows


class RunWindowsKeepAwakeTests(unittest.TestCase):
    def test_start_keep_awake_records_pid_when_process_stays_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            helper_script = tmp_path / "keep_awake.py"
            pid_file = tmp_path / "keep_awake.pid"
            helper_script.write_text("print('ok')\n", encoding="utf-8")

            with patch.object(run_windows, "KEEP_AWAKE_SCRIPT", helper_script), patch.object(
                run_windows,
                "KEEP_AWAKE_PID_FILE",
                pid_file,
            ), patch.object(
                run_windows,
                "KEEP_AWAKE_STDOUT_LOG",
                tmp_path / "keep_awake.out.log",
            ), patch.object(
                run_windows,
                "KEEP_AWAKE_STDERR_LOG",
                tmp_path / "keep_awake.err.log",
            ), patch("run_windows.is_keep_awake_enabled", return_value=True), patch(
                "run_windows.get_keep_awake_pid",
                side_effect=[None, 4321],
            ), patch(
                "run_windows.launch_detached_process",
                return_value=SimpleNamespace(pid=4321),
            ), patch("run_windows.time.sleep", return_value=None):
                pid = run_windows.start_keep_awake()

            self.assertEqual(pid, 4321)
            self.assertEqual(pid_file.read_text(encoding="utf-8"), "4321")

    def test_start_bot_starts_keep_awake_when_bot_is_already_running(self) -> None:
        with patch("run_windows.get_running_pid", return_value=1234), patch(
            "run_windows.start_keep_awake"
        ) as start_keep_awake:
            run_windows.start_bot()

        start_keep_awake.assert_called_once_with()

    def test_stop_bot_stops_keep_awake_with_bot(self) -> None:
        with patch("run_windows.get_running_pid", return_value=1234), patch(
            "run_windows.get_keep_awake_pid",
            return_value=5678,
        ), patch("run_windows.stop_process", return_value=1234) as stop_process, patch(
            "run_windows.stop_keep_awake"
        ) as stop_keep_awake:
            run_windows.stop_bot()

        stop_process.assert_called_once_with(run_windows.PID_FILE, "Telegram bot")
        stop_keep_awake.assert_called_once_with(quiet_if_missing=True)


if __name__ == "__main__":
    unittest.main()
