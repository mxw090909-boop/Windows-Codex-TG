#!/usr/bin/env python3
import ctypes
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = SCRIPT_DIR / ".runtime"
BOT_SCRIPT = SCRIPT_DIR / "tg_codex_bot.py"
PID_FILE = RUNTIME_DIR / "bot.pid"
STDOUT_LOG = RUNTIME_DIR / "bot.out.log"
STDERR_LOG = RUNTIME_DIR / "bot.err.log"
KEEP_AWAKE_SCRIPT = SCRIPT_DIR / "keep_awake.py"
KEEP_AWAKE_PID_FILE = RUNTIME_DIR / "keep_awake.pid"
KEEP_AWAKE_STDOUT_LOG = RUNTIME_DIR / "keep_awake.out.log"
KEEP_AWAKE_STDERR_LOG = RUNTIME_DIR / "keep_awake.err.log"
STATE_PATH = RUNTIME_DIR / "bot_state.json"
LOCAL_ENV_PATH = SCRIPT_DIR / "telegram.local.env"
LOCAL_CODEX_DIR = RUNTIME_DIR / "codex-bin"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                reconfigure(errors="replace")
            except Exception:
                pass


def info(message: str) -> None:
    print(f"[info] {message}")


def ok(message: str) -> None:
    print(f"[ok] {message}")


def warn(message: str) -> None:
    print(f"[warn] {message}")


def fail(message: str) -> None:
    print(f"[error] {message}", file=sys.stderr)
    raise SystemExit(1)


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def read_pid_file(path: Path) -> Optional[int]:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        path.unlink(missing_ok=True)
        return None
    try:
        return int(raw)
    except ValueError:
        path.unlink(missing_ok=True)
        return None


def load_local_env(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


LOCAL_ENV = load_local_env(LOCAL_ENV_PATH)


def env_value(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is not None and raw.strip():
        return raw.strip()
    file_value = LOCAL_ENV.get(name, "").strip()
    if file_value:
        return file_value
    return default


def resolve_path(raw: str, default: Path) -> Path:
    candidate = Path(raw).expanduser() if raw else default
    if not candidate.is_absolute():
        candidate = (SCRIPT_DIR / candidate).resolve()
    return candidate


def resolve_codex_bin() -> str:
    configured = env_value("CODEX_BIN")
    if configured:
        return ensure_usable_codex_bin(configured)
    found = shutil.which("codex")
    if found:
        return ensure_usable_codex_bin(found)
    return "codex"


def ensure_usable_codex_bin(raw_path: str) -> str:
    candidate = Path(raw_path)
    if os.name != "nt":
        return str(candidate)
    normalized = str(candidate).lower()
    if "\\windowsapps\\" not in normalized:
        return str(candidate)
    return str(prepare_local_codex_copy(candidate))


def prepare_local_codex_copy(source_codex: Path) -> Path:
    LOCAL_CODEX_DIR.mkdir(parents=True, exist_ok=True)

    source_dir = source_codex.parent
    files_to_copy = (
        "codex.exe",
        "codex-command-runner.exe",
        "codex-windows-sandbox-setup.exe",
    )

    for name in files_to_copy:
        src = source_dir / name
        if not src.exists():
            continue
        dst = LOCAL_CODEX_DIR / name
        if (not dst.exists()) or (src.stat().st_size != dst.stat().st_size) or (src.stat().st_mtime > dst.stat().st_mtime):
            shutil.copy2(src, dst)

    native_src = source_dir / "native"
    native_dst = LOCAL_CODEX_DIR / "native"
    if native_src.exists():
        shutil.copytree(native_src, native_dst, dirs_exist_ok=True)

    copied_codex = LOCAL_CODEX_DIR / "codex.exe"
    if not copied_codex.exists():
        fail(f"没能准备好本地 Codex CLI 副本: {copied_codex}")
    return copied_codex


def resolve_session_root() -> Path:
    configured = env_value("CODEX_SESSION_ROOT")
    if configured:
        return resolve_path(configured, Path.home() / ".codex" / "sessions")
    return Path.home() / ".codex" / "sessions"


def read_pid() -> Optional[int]:
    return read_pid_file(PID_FILE)


def is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def get_running_pid() -> Optional[int]:
    return get_running_pid_for(PID_FILE)


def get_running_pid_for(pid_file: Path) -> Optional[int]:
    pid = read_pid_file(pid_file)
    if pid is None:
        return None
    if is_process_running(pid):
        return pid
    pid_file.unlink(missing_ok=True)
    return None


def get_keep_awake_pid() -> Optional[int]:
    return get_running_pid_for(KEEP_AWAKE_PID_FILE)


def tail_lines(path: Path, limit: int = 40) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-limit:])


def show_recent_logs() -> None:
    seen_any = False
    for path in (STDOUT_LOG, STDERR_LOG, KEEP_AWAKE_STDOUT_LOG, KEEP_AWAKE_STDERR_LOG):
        if not path.exists():
            continue
        content = tail_lines(path)
        if not content:
            continue
        seen_any = True
        print()
        print(f"===== {path} =====")
        print(content)
    if not seen_any:
        info("还没有日志。")


def probe_tg_local_voice_env() -> tuple[bool, bool]:
    has_whisper = importlib.util.find_spec("whisper") is not None
    has_ffmpeg = bool(shutil.which("ffmpeg"))
    if not has_ffmpeg:
        try:
            import imageio_ffmpeg

            imageio_ffmpeg.get_ffmpeg_exe()
            has_ffmpeg = True
        except Exception:
            has_ffmpeg = False
    return has_whisper, has_ffmpeg


def configure_tg_voice_defaults(config: Dict[str, str]) -> Dict[str, str]:
    enabled_raw = env_value("TG_VOICE_TRANSCRIBE_ENABLED")
    has_whisper, has_ffmpeg = probe_tg_local_voice_env()
    backend = (config.get("TG_VOICE_TRANSCRIBE_BACKEND") or "local-whisper").strip().lower()

    if not enabled_raw:
        if has_whisper and has_ffmpeg:
            config["TG_VOICE_TRANSCRIBE_ENABLED"] = "1"
            config["TG_VOICE_TRANSCRIBE_BACKEND"] = "local-whisper"
            info("检测到本地 Whisper 环境，已默认启用 Telegram 本地语音转写")
        else:
            config["TG_VOICE_TRANSCRIBE_ENABLED"] = "0"
            warn("本地语音转写环境还没就绪，Telegram 语音转写暂时保持关闭")
            if not has_whisper:
                warn("缺少 whisper/torch 依赖，安装命令：python -m pip install -U openai-whisper torch")
            if not has_ffmpeg:
                warn("缺少 ffmpeg，安装命令：python -m pip install -U imageio-ffmpeg")
        return config

    if config.get("TG_VOICE_TRANSCRIBE_ENABLED") == "1" and backend == "local-whisper":
        if not has_whisper or not has_ffmpeg:
            warn("已显式启用本地语音转写，但本地环境还没装好；bot 启动后会继续保持禁用状态")
            if not has_whisper:
                warn("缺少 whisper/torch 依赖，安装命令：python -m pip install -U openai-whisper torch")
            if not has_ffmpeg:
                warn("缺少 ffmpeg，安装命令：python -m pip install -U imageio-ffmpeg")
    return config


def validate_start_config() -> Dict[str, str]:
    token = env_value("TELEGRAM_BOT_TOKEN")
    if not token:
        fail("缺少 TELEGRAM_BOT_TOKEN。把它填进 telegram.local.env 里。")
    if not re.fullmatch(r"[0-9]{6,}:[A-Za-z0-9_-]{20,}", token):
        fail("TELEGRAM_BOT_TOKEN 格式不对，正常应该像 '123456789:ABCDEF...'。")

    require_allowlist = env_value("TG_REQUIRE_ALLOWLIST", "1")
    allowed_user_ids = env_value("ALLOWED_TELEGRAM_USER_IDS")
    if require_allowlist != "0" and not allowed_user_ids:
        fail("默认要求 ALLOWED_TELEGRAM_USER_IDS。把你自己的 Telegram 数字 user id 填进去。")
    if allowed_user_ids and not re.fullmatch(r"[0-9]+(,[0-9]+)*", allowed_user_ids):
        fail("ALLOWED_TELEGRAM_USER_IDS 格式不对，只能是数字，多个用英文逗号。")

    default_cwd = resolve_path(env_value("DEFAULT_CWD"), SCRIPT_DIR)
    if not default_cwd.is_dir():
        fail(f"DEFAULT_CWD 不存在或不是目录: {default_cwd}")

    codex_bin = resolve_codex_bin()
    if ("\\" in codex_bin or "/" in codex_bin) and not Path(codex_bin).exists():
        fail(f"CODEX_BIN 不存在: {codex_bin}")

    config = {
        "TELEGRAM_BOT_TOKEN": token,
        "ALLOWED_TELEGRAM_USER_IDS": allowed_user_ids,
        "TG_REQUIRE_ALLOWLIST": require_allowlist,
        "DEFAULT_CWD": str(default_cwd),
        "CODEX_BIN": codex_bin,
        "CODEX_SESSION_ROOT": str(resolve_session_root()),
        "CODEX_SANDBOX_MODE": env_value("CODEX_SANDBOX_MODE"),
        "CODEX_APPROVAL_POLICY": env_value("CODEX_APPROVAL_POLICY"),
        "CODEX_DANGEROUS_BYPASS": env_value("CODEX_DANGEROUS_BYPASS", "0"),
        "CODEX_IDLE_TIMEOUT_SEC": env_value("CODEX_IDLE_TIMEOUT_SEC", "3600"),
        "STATE_PATH": str(STATE_PATH),
        "TELEGRAM_INSECURE_SKIP_VERIFY": env_value("TELEGRAM_INSECURE_SKIP_VERIFY", "0"),
        "TELEGRAM_CA_BUNDLE": env_value("TELEGRAM_CA_BUNDLE"),
        "TG_STREAM_ENABLED": env_value("TG_STREAM_ENABLED", "1"),
        "TG_STREAM_EDIT_INTERVAL_MS": env_value("TG_STREAM_EDIT_INTERVAL_MS", "300"),
        "TG_STREAM_MIN_DELTA_CHARS": env_value("TG_STREAM_MIN_DELTA_CHARS", "8"),
        "TG_THINKING_STATUS_INTERVAL_MS": env_value("TG_THINKING_STATUS_INTERVAL_MS", "700"),
        "TG_VOICE_TRANSCRIBE_ENABLED": env_value("TG_VOICE_TRANSCRIBE_ENABLED"),
        "TG_VOICE_TRANSCRIBE_BACKEND": env_value("TG_VOICE_TRANSCRIBE_BACKEND", "local-whisper"),
        "TG_VOICE_TRANSCRIBE_MODEL": env_value("TG_VOICE_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
        "TG_VOICE_TRANSCRIBE_TIMEOUT_SEC": env_value("TG_VOICE_TRANSCRIBE_TIMEOUT_SEC", "180"),
        "TG_VOICE_MAX_BYTES": env_value("TG_VOICE_MAX_BYTES", "26214400"),
        "TG_VOICE_LOCAL_MODEL": env_value("TG_VOICE_LOCAL_MODEL", "base"),
        "TG_VOICE_LOCAL_DEVICE": env_value("TG_VOICE_LOCAL_DEVICE", "cpu"),
        "TG_VOICE_LOCAL_LANGUAGE": env_value("TG_VOICE_LOCAL_LANGUAGE"),
        "TG_VOICE_FFMPEG_BIN": env_value("TG_VOICE_FFMPEG_BIN"),
        "OPENAI_API_KEY": env_value("OPENAI_API_KEY"),
        "OPENAI_BASE_URL": env_value("OPENAI_BASE_URL"),
    }
    passthrough_prefixes = ("TG_", "OPENAI_", "TELEGRAM_", "MEMORY_")
    for key in LOCAL_ENV.keys():
        if key in config:
            continue
        if not key.startswith(passthrough_prefixes):
            continue
        value = env_value(key)
        if value:
            config[key] = value
    return configure_tg_voice_defaults(config)


def is_keep_awake_enabled() -> bool:
    return env_value("KEEP_AWAKE_ENABLED", "1") != "0"


def launch_detached_process(
    args: list[str],
    env: Dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
) -> subprocess.Popen:
    stdout_path.touch()
    stderr_path.touch()

    stdout_handle = stdout_path.open("a", encoding="utf-8")
    stderr_handle = stderr_path.open("a", encoding="utf-8")

    try:
        return subprocess.Popen(
            args,
            cwd=str(SCRIPT_DIR),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()


def stop_process(pid_file: Path, label: str) -> Optional[int]:
    running_pid = get_running_pid_for(pid_file)
    if running_pid is None:
        return None

    subprocess.run(["taskkill", "/PID", str(running_pid), "/T", "/F"], capture_output=True, text=True)
    pid_file.unlink(missing_ok=True)
    ok(f"{label} 已停止，PID={running_pid}")
    return running_pid


def start_keep_awake() -> Optional[int]:
    ensure_runtime_dir()

    if not is_keep_awake_enabled():
        info("keep-awake helper 已禁用（KEEP_AWAKE_ENABLED=0）。")
        return None

    running_pid = get_keep_awake_pid()
    if running_pid is not None:
        info(f"keep-awake helper 已经在跑了，PID={running_pid}")
        return running_pid

    if not KEEP_AWAKE_SCRIPT.exists():
        fail(f"找不到 keep_awake.py: {KEEP_AWAKE_SCRIPT}")

    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"

    info("正在启动 keep-awake helper...")
    process = launch_detached_process(
        [sys.executable, "-u", str(KEEP_AWAKE_SCRIPT)],
        child_env,
        KEEP_AWAKE_STDOUT_LOG,
        KEEP_AWAKE_STDERR_LOG,
    )

    KEEP_AWAKE_PID_FILE.write_text(str(process.pid), encoding="utf-8")
    time.sleep(1)

    started_pid = get_keep_awake_pid()
    if started_pid is not None:
        ok(f"keep-awake helper 已启动，PID={started_pid}")
        return started_pid

    KEEP_AWAKE_PID_FILE.unlink(missing_ok=True)
    fail("keep-awake helper 启动后立刻退出了。")


def stop_keep_awake(quiet_if_missing: bool = False) -> None:
    stopped_pid = stop_process(KEEP_AWAKE_PID_FILE, "keep-awake helper")
    if stopped_pid is None and not quiet_if_missing:
        info("keep-awake helper 没在运行。")


def start_bot() -> None:
    ensure_runtime_dir()

    running_pid = get_running_pid()
    if running_pid is not None:
        info(f"Telegram bot 已经在跑了，PID={running_pid}")
        start_keep_awake()
        return

    child_env = os.environ.copy()
    child_env.update(validate_start_config())
    child_env["PYTHONUTF8"] = "1"

    info("正在启动 Telegram bot...")
    process = launch_detached_process(
        [sys.executable, "-u", str(BOT_SCRIPT)],
        child_env,
        STDOUT_LOG,
        STDERR_LOG,
    )

    PID_FILE.write_text(str(process.pid), encoding="utf-8")
    time.sleep(2)

    started_pid = get_running_pid()
    if started_pid is not None:
        try:
            start_keep_awake()
        except SystemExit:
            stop_process(PID_FILE, "Telegram bot")
            raise
        ok(f"Telegram bot 已启动，PID={started_pid}")
        ok(f"stdout: {STDOUT_LOG}")
        ok(f"stderr: {STDERR_LOG}")
        return

    PID_FILE.unlink(missing_ok=True)
    fail("Telegram bot 启动后立刻退出了。")


def stop_bot() -> None:
    running_pid = get_running_pid()
    keep_awake_pid = get_keep_awake_pid()
    if running_pid is None and keep_awake_pid is None:
        info("Telegram bot 没在运行。")
        return

    if running_pid is not None:
        stop_process(PID_FILE, "Telegram bot")
    else:
        info("Telegram bot 没在运行。")

    if keep_awake_pid is not None:
        stop_keep_awake(quiet_if_missing=True)
    else:
        info("keep-awake helper 没在运行。")


def show_status() -> None:
    running_pid = get_running_pid()
    keep_awake_pid = get_keep_awake_pid()

    if running_pid is None:
        info("Telegram bot 没在运行。")
    else:
        ok(f"Telegram bot 运行中，PID={running_pid}")
        info(f"stdout: {STDOUT_LOG}")
        info(f"stderr: {STDERR_LOG}")

    if not is_keep_awake_enabled():
        info("keep-awake helper 已禁用（KEEP_AWAKE_ENABLED=0）。")
    elif keep_awake_pid is None:
        warn("keep-awake helper 没在运行。")
    else:
        ok(f"keep-awake helper 运行中，PID={keep_awake_pid}")
        info(f"stdout: {KEEP_AWAKE_STDOUT_LOG}")
        info(f"stderr: {KEEP_AWAKE_STDERR_LOG}")


def follow_logs(paths: Iterable[Path]) -> None:
    offsets = {path: path.stat().st_size if path.exists() else 0 for path in paths}
    try:
        while True:
            changed = False
            for path in paths:
                if not path.exists():
                    continue
                current_size = path.stat().st_size
                previous_size = offsets.get(path, 0)
                if current_size < previous_size:
                    previous_size = 0
                if current_size == previous_size:
                    offsets[path] = current_size
                    continue

                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(previous_size)
                    chunk = handle.read()
                    offsets[path] = handle.tell()

                if chunk:
                    changed = True
                    print()
                    print(f"===== {path} =====")
                    print(chunk, end="" if chunk.endswith("\n") else "\n")

            if not changed:
                time.sleep(1)
    except KeyboardInterrupt:
        print()
        info("停止查看日志。")


def show_logs() -> None:
    ensure_runtime_dir()
    STDOUT_LOG.touch()
    STDERR_LOG.touch()
    KEEP_AWAKE_STDOUT_LOG.touch()
    KEEP_AWAKE_STDERR_LOG.touch()
    show_recent_logs()
    follow_logs((STDOUT_LOG, STDERR_LOG, KEEP_AWAKE_STDOUT_LOG, KEEP_AWAKE_STDERR_LOG))


def show_help() -> None:
    print("Windows Telegram launcher for codex-tg")
    print()
    print("Usage:")
    print(r"  .\run.ps1 start")
    print(r"  .\run.ps1 stop")
    print(r"  .\run.ps1 status")
    print(r"  .\run.ps1 logs")
    print(r"  .\run.ps1 restart")
    print()
    print("Start 会顺手拉起 keep_awake.py；想关掉可设 KEEP_AWAKE_ENABLED=0。")
    print()
    print("Before start:")
    print("  1. Fill telegram.local.env")
    print("  2. Put TELEGRAM_BOT_TOKEN there")
    print("  3. Put your numeric ALLOWED_TELEGRAM_USER_IDS there")


def main() -> None:
    configure_stdio()
    command = sys.argv[1].strip().lower() if len(sys.argv) > 1 else "start"
    if command == "start":
        start_bot()
    elif command == "stop":
        stop_bot()
    elif command == "status":
        show_status()
    elif command == "logs":
        show_logs()
    elif command == "restart":
        stop_bot()
        start_bot()
    elif command == "help":
        show_help()
    else:
        fail(f"不支持的命令: {command}")


if __name__ == "__main__":
    main()
