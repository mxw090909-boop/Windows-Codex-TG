#!/usr/bin/env python3
import json
import mimetypes
import os
import random
import re
import signal
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from codex_common import (
    BotState,
    CodexRunner,
    MemoryStore,
    RunningPromptRegistry,
    SessionStore,
    chunk_text,
    ensure_stdio_encoding,
    env,
    log,
    parse_bool_env,
    parse_dangerous_bypass_level,
    parse_non_negative_int,
    resolve_codex_bin,
)
from tg_tts import (
    DEFAULT_MINIMAX_API_BASE,
    DEFAULT_MINIMAX_MODEL,
    DEFAULT_TTS_MAX_CHARS,
    LocalGptSovitsTtsSynthesizer,
    MiniMaxTtsSynthesizer,
    SynthesizedVoiceNote,
    is_tts_reply_candidate,
)


MAX_TELEGRAM_TEXT = 4096
MAX_TELEGRAM_ATTACHMENT_BYTES = 25 * 1024 * 1024
SCRIPT_DIR = Path(__file__).resolve().parent
BOT_COMMANDS: List[Dict[str, str]] = [
    {"command": "start", "description": "开始使用"},
    {"command": "help", "description": "查看帮助"},
    {"command": "sessions", "description": "查看最近会话"},
    {"command": "use", "description": "切换会话"},
    {"command": "history", "description": "查看会话历史"},
    {"command": "new", "description": "新建会话模式"},
    {"command": "status", "description": "查看当前会话"},
    {"command": "ask", "description": "在当前会话提问"},
]

BOT_COMMANDS.append({"command": "heartbeat", "description": "定时主动来找你"})
BOT_COMMANDS.append({"command": "memory", "description": "查看和管理记忆"})
BOT_COMMANDS.append({"command": "voice", "description": "配置语音回复"})

HEARTBEAT_MIN_INTERVAL_SEC = 60
HEARTBEAT_DEFAULT_INTERVAL_SEC = 30 * 60
HEARTBEAT_POLL_INTERVAL_SEC = 15
HEARTBEAT_CONVERSATION_COOLDOWN_SEC = 10 * 60
HEARTBEAT_MAX_UNANSWERED = 4
TOKYO_TZ = timezone(timedelta(hours=9), name="Asia/Tokyo")
HEARTBEAT_SESSION_PROMPT = ""
HEARTBEAT_BANNED_PATTERNS: List[str] = []
HEARTBEAT_TEMPLATE_MESSAGES: List[str] = []
HEARTBEAT_FOLLOWUP_TEMPLATE_MESSAGES: List[str] = []
CONVERSATION_TARGET_CHARS = 110
CONVERSATION_MAX_CHARS = 180
CONVERSATION_MIN_SPLIT_CHARS = 90
CONVERSATION_MAX_UNITS_PER_PART = 2
CONVERSATION_CODE_BLOCK_MAX_CHARS = 1200
CONVERSATION_PART_DELAY_SEC = 0.18
MEMORY_MAX_PROMPT_ITEMS = 5
MEMORY_MAX_RECENT_ITEMS = 2
MEMORY_MAX_LIST_ITEMS = 12
MEMORY_WRITEBACK_MAX_INPUT_CHARS = 900
MEMORY_SEARCH_QUERY_MAX_CHARS = 500
MEMORY_ALLOWED_CATEGORIES = {"profile", "preference", "project", "ongoing", "boundary", "relationship", "general"}
MEMORY_CATEGORY_LABELS = {
    "profile": "个人",
    "preference": "偏好",
    "project": "项目",
    "ongoing": "近况",
    "boundary": "边界",
    "relationship": "关系",
    "general": "记忆",
}
TTS_CALLBACK_PREFIX = "tts:"
TTS_BUTTON_TEXT = "听我说"
TTS_FREQUENCY_DEFAULT = "medium"
TTS_FREQUENCY_LABELS = {
    "high": "高",
    "medium": "中",
    "low": "低",
}
NEW_THREAD_PERSONA_PROMPT = ""
MEMORY_CONTEXT_PROMPT = ""
MEMORY_WRITEBACK_PROMPT = ""


def _resolve_local_path(raw_path: Optional[str]) -> Optional[Path]:
    if not raw_path:
        return None
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = (SCRIPT_DIR / candidate).resolve()
    return candidate


def _read_local_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _split_override_items(raw: str) -> List[str]:
    if "\n" in raw or "\r" in raw:
        pieces = raw.splitlines()
    else:
        pieces = raw.split("||")
    return [piece.strip() for piece in pieces if piece.strip()]


def _load_text_override(default: str, *, inline_env_name: str, path_env_name: str) -> str:
    inline_value = env(inline_env_name)
    if inline_value:
        return inline_value.strip()
    path = _resolve_local_path(env(path_env_name))
    if path is None:
        return default
    if not path.exists():
        log(f"[warn] {path_env_name} points to a missing file: {path}")
        return default
    try:
        return _read_local_text(path) or default
    except Exception as e:
        log(f"[warn] failed to read {path_env_name} from {path}: {e}")
        return default


def _load_list_override(default: List[str], *, inline_env_name: str, path_env_name: str) -> List[str]:
    inline_value = env(inline_env_name)
    if inline_value:
        values = _split_override_items(inline_value)
        return values or list(default)
    path = _resolve_local_path(env(path_env_name))
    if path is None:
        return list(default)
    if not path.exists():
        log(f"[warn] {path_env_name} points to a missing file: {path}")
        return list(default)
    try:
        values = _split_override_items(_read_local_text(path))
        return values or list(default)
    except Exception as e:
        log(f"[warn] failed to read {path_env_name} from {path}: {e}")
        return list(default)


def _normalize_tts_frequency(value: Any) -> str:
    raw = str(value or "").strip().lower()
    mapping = {
        "high": "high",
        "h": "high",
        "高": "high",
        "medium": "medium",
        "mid": "medium",
        "m": "medium",
        "中": "medium",
        "normal": "medium",
        "default": "medium",
        "low": "low",
        "l": "low",
        "低": "low",
    }
    return mapping.get(raw, TTS_FREQUENCY_DEFAULT)


def _is_known_tts_frequency(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return raw in {"high", "h", "高", "medium", "mid", "m", "中", "normal", "default", "low", "l", "低"}


def _render_prompt_template(template: str, **values: str) -> str:
    rendered = (template or "").strip()
    if not rendered:
        return ""
    for key, value in values.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered.strip()

def parse_allowed_user_ids(raw: Optional[str]) -> Optional[Set[int]]:
    if not raw:
        return None
    result: Set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            raise ValueError(f"invalid user id in ALLOWED_TELEGRAM_USER_IDS: {part}")
    return result

class TelegramAPI:
    def __init__(
        self,
        token: str,
        ca_bundle: Optional[str] = None,
        insecure_skip_verify: bool = False,
    ):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_base_url = f"https://api.telegram.org/file/bot{token}"
        self.ssl_context: Optional[ssl.SSLContext] = None
        if insecure_skip_verify:
            self.ssl_context = ssl._create_unverified_context()
        elif ca_bundle:
            self.ssl_context = ssl.create_default_context(cafile=ca_bundle)

    def _request(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.base_url}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=80, context=self.ssl_context) as resp:
            raw = resp.read().decode("utf-8")
        parsed = json.loads(raw)
        if not parsed.get("ok"):
            raise RuntimeError(f"telegram api error for {method}: {raw}")
        return parsed["result"]

    def _multipart_request(
        self,
        method: str,
        *,
        fields: Dict[str, Any],
        files: Optional[List[Tuple[str, str, bytes, str]]] = None,
    ) -> Dict[str, Any]:
        boundary = f"----codex-tg-{uuid.uuid4().hex}"
        body = bytearray()

        for name, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")

        for field_name, file_name, content, content_type in files or []:
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{file_name}"\r\n'
                ).encode("utf-8")
            )
            body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
            body.extend(content)
            body.extend(b"\r\n")

        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        req = urllib.request.Request(
            url=f"{self.base_url}/{method}",
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=240, context=self.ssl_context) as resp:
            raw = resp.read().decode("utf-8")
        parsed = json.loads(raw)
        if not parsed.get("ok"):
            raise RuntimeError(f"telegram api error for {method}: {raw}")
        return parsed["result"]

    def get_updates(self, offset: Optional[int], timeout: int = 30) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        return self._request("getUpdates", payload)

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        for part in chunk_text(text, size=min(3800, MAX_TELEGRAM_TEXT)):
            self.send_message_with_result(
                chat_id=chat_id,
                text=part,
                reply_to=reply_to,
                reply_markup=reply_markup,
            )

    def send_message_with_result(
        self,
        chat_id: int,
        text: str,
        reply_to: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            payload["reply_to_message_id"] = reply_to
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._request("sendMessage", payload)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._request("editMessageText", payload)

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self._request("sendChatAction", {"chat_id": chat_id, "action": action})

    def set_my_commands(self, commands: List[Dict[str, str]]) -> None:
        self._request("setMyCommands", {"commands": commands})

    def set_chat_menu_button_commands(self) -> None:
        self._request("setChatMenuButton", {"menu_button": {"type": "commands"}})

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> None:
        payload: Dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            payload["text"] = text
        self._request("answerCallbackQuery", payload)

    def get_file(self, file_id: str) -> Dict[str, Any]:
        return self._request("getFile", {"file_id": file_id})

    def download_file_bytes(self, file_path: str) -> bytes:
        quoted_path = urllib.parse.quote(file_path.lstrip("/"), safe="/")
        req = urllib.request.Request(url=f"{self.file_base_url}/{quoted_path}", method="GET")
        with urllib.request.urlopen(req, timeout=120, context=self.ssl_context) as resp:
            return resp.read()

    def send_voice_with_result(
        self,
        *,
        chat_id: int,
        voice: SynthesizedVoiceNote,
        reply_to: Optional[int] = None,
    ) -> Dict[str, Any]:
        fields: Dict[str, Any] = {"chat_id": chat_id}
        if reply_to is not None:
            fields["reply_to_message_id"] = reply_to
        if voice.duration_seconds is not None:
            fields["duration"] = voice.duration_seconds
        return self._multipart_request(
            "sendVoice",
            fields=fields,
            files=[
                (
                    "voice",
                    voice.file_name,
                    voice.audio_bytes,
                    voice.mime_type,
                )
            ],
        )

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self._request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def normalize_audio_filename(file_name: Optional[str], mime_type: Optional[str]) -> Tuple[str, str]:
    name = (file_name or "").strip() or "telegram-voice.ogg"
    suffix = Path(name).suffix.lower()
    if suffix == ".oga":
        name = f"{Path(name).stem}.ogg"
        suffix = ".ogg"
    if not suffix:
        guessed_suffix = mimetypes.guess_extension(mime_type or "") or ".ogg"
        if guessed_suffix == ".oga":
            guessed_suffix = ".ogg"
        name = f"{name}{guessed_suffix}"
    content_type = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    if content_type == "audio/x-wav":
        content_type = "audio/wav"
    return name, content_type


def fetch_telegram_audio(
    api: TelegramAPI,
    *,
    file_id: str,
    file_name: Optional[str],
    mime_type: Optional[str],
    file_size: Optional[int],
    max_bytes: int,
) -> Tuple[bytes, str, str]:
    if file_size and file_size > max_bytes:
        raise RuntimeError(f"语音文件过大（{file_size} bytes），超过当前限制 {max_bytes} bytes。")

    file_meta = api.get_file(file_id)
    file_path = str(file_meta.get("file_path") or "").strip()
    if not file_path:
        raise RuntimeError("Telegram 未返回可下载的 file_path。")

    audio_bytes = api.download_file_bytes(file_path)
    if not audio_bytes:
        raise RuntimeError("下载到的语音文件为空。")
    if len(audio_bytes) > max_bytes:
        raise RuntimeError(f"语音文件过大（{len(audio_bytes)} bytes），超过当前限制 {max_bytes} bytes。")

    normalized_name, content_type = normalize_audio_filename(
        file_name or Path(file_path).name,
        mime_type,
    )
    return audio_bytes, normalized_name, content_type


def normalize_attachment_filename(
    file_name: Optional[str],
    mime_type: Optional[str],
    *,
    default_stem: str,
    default_suffix: str = "",
) -> Tuple[str, str]:
    raw_name = Path((file_name or "").strip()).name
    raw_name = re.sub(r'[<>:"/\\\\|?*\x00-\x1f]+', "_", raw_name).strip(" .")
    suffix = Path(raw_name).suffix.lower()
    if not suffix:
        guessed_suffix = mimetypes.guess_extension(mime_type or "") or default_suffix
        if guessed_suffix == ".jpe":
            guessed_suffix = ".jpg"
        suffix = guessed_suffix or default_suffix

    stem = Path(raw_name).stem if raw_name else default_stem
    stem = re.sub(r"\s+", "-", stem).strip(" .-_")
    stem = stem[:80] or default_stem
    normalized_name = f"{stem}{suffix}" if suffix else stem
    content_type = mime_type or mimetypes.guess_type(normalized_name)[0] or "application/octet-stream"
    return normalized_name, content_type


def fetch_telegram_file(
    api: TelegramAPI,
    *,
    file_id: str,
    file_name: Optional[str],
    mime_type: Optional[str],
    file_size: Optional[int],
    max_bytes: int,
    default_stem: str,
    default_suffix: str = "",
) -> Tuple[bytes, str, str, str]:
    if file_size and file_size > max_bytes:
        raise RuntimeError(f"文件过大（{file_size} bytes），超过当前限制 {max_bytes} bytes。")

    file_meta = api.get_file(file_id)
    file_path = str(file_meta.get("file_path") or "").strip()
    if not file_path:
        raise RuntimeError("Telegram 未返回可下载的 file_path。")

    file_bytes = api.download_file_bytes(file_path)
    if not file_bytes:
        raise RuntimeError("下载到的文件为空。")
    if len(file_bytes) > max_bytes:
        raise RuntimeError(f"文件过大（{len(file_bytes)} bytes），超过当前限制 {max_bytes} bytes。")

    normalized_name, content_type = normalize_attachment_filename(
        file_name or Path(file_path).name,
        mime_type,
        default_stem=default_stem,
        default_suffix=default_suffix,
    )
    return file_bytes, normalized_name, content_type, file_path


@dataclass
class TelegramAttachment:
    local_path: Path
    display_name: str
    mime_type: str
    size_bytes: int
    is_image: bool
    kind: str


class AudioTranscriber:
    def transcribe_telegram_audio(
        self,
        api: TelegramAPI,
        *,
        file_id: str,
        file_name: Optional[str],
        mime_type: Optional[str],
        file_size: Optional[int],
    ) -> str:
        raise NotImplementedError


class OpenAIAudioTranscriber(AudioTranscriber):
    def __init__(
        self,
        api_key: str,
        model: str,
        api_base: str = "https://api.openai.com/v1",
        timeout_sec: int = 180,
        max_bytes: int = 25 * 1024 * 1024,
    ):
        self.api_key = api_key
        self.model = model
        self.api_base = api_base.rstrip("/")
        self.timeout_sec = max(30, int(timeout_sec))
        self.max_bytes = max(1, int(max_bytes))

    @staticmethod
    def _build_multipart_body(
        *,
        fields: Dict[str, str],
        file_field: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> Tuple[bytes, str]:
        boundary = f"----CodexTgBoundary{uuid.uuid4().hex}"
        body = bytearray()
        for key, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
            )
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        body.extend(content)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        return bytes(body), boundary

    def transcribe_telegram_audio(
        self,
        api: TelegramAPI,
        *,
        file_id: str,
        file_name: Optional[str],
        mime_type: Optional[str],
        file_size: Optional[int],
    ) -> str:
        audio_bytes, normalized_name, content_type = fetch_telegram_audio(
            api,
            file_id=file_id,
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
            max_bytes=self.max_bytes,
        )
        body, boundary = self._build_multipart_body(
            fields={"model": self.model},
            file_field="file",
            filename=normalized_name,
            content=audio_bytes,
            content_type=content_type,
        )
        req = urllib.request.Request(
            url=f"{self.api_base}/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"转写请求失败: HTTP {e.code} {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"转写请求失败: {e}") from e

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError("转写接口返回了无法解析的响应。") from e

        text = str(parsed.get("text") or "").strip()
        if not text:
            raise RuntimeError("转写成功，但没有返回文本。")
        return text


class LocalWhisperAudioTranscriber(AudioTranscriber):
    def __init__(
        self,
        model_name: str,
        ffmpeg_bin: Optional[str] = None,
        device: Optional[str] = None,
        language: Optional[str] = None,
        max_bytes: int = 25 * 1024 * 1024,
    ):
        self.model_name = model_name
        self.ffmpeg_bin = ffmpeg_bin
        self.device = device
        self.language = language
        self.max_bytes = max(1, int(max_bytes))
        self._model = None
        self._lock = threading.Lock()

    def validate_environment(self) -> None:
        try:
            import whisper  # noqa: F401
        except Exception as e:
            raise RuntimeError("本地转写需要安装 whisper Python 包。") from e
        self._resolve_ffmpeg_bin()

    def _resolve_ffmpeg_bin(self) -> str:
        configured = (self.ffmpeg_bin or "").strip()
        if configured:
            if Path(configured).exists():
                return configured
            raise RuntimeError(f"找不到 ffmpeg: {configured}")
        found = shutil.which("ffmpeg")
        if found:
            return found
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as e:
            raise RuntimeError("本地转写需要 ffmpeg，可安装系统 ffmpeg 或提供 TG_VOICE_FFMPEG_BIN。") from e

    def _load_model(self):
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                import whisper
            except Exception as e:
                raise RuntimeError("本地转写需要安装 whisper Python 包。") from e
            try:
                self._model = whisper.load_model(self.model_name, device=self.device)
            except Exception as e:
                raise RuntimeError(f"加载本地 Whisper 模型失败: {e}") from e
            return self._model

    def _decode_audio(self, file_path: str):
        ffmpeg_bin = self._resolve_ffmpeg_bin()
        try:
            import numpy as np
            import whisper.audio as whisper_audio
        except Exception as e:
            raise RuntimeError("本地转写缺少 numpy/whisper 依赖。") from e

        cmd = [
            ffmpeg_bin,
            "-nostdin",
            "-threads",
            "0",
            "-i",
            file_path,
            "-f",
            "s16le",
            "-ac",
            "1",
            "-acodec",
            "pcm_s16le",
            "-ar",
            str(whisper_audio.SAMPLE_RATE),
            "-",
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, check=True).stdout
        except subprocess.CalledProcessError as e:
            detail = e.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ffmpeg 解码失败: {detail}") from e
        return np.frombuffer(out, np.int16).flatten().astype("float32") / 32768.0

    def transcribe_telegram_audio(
        self,
        api: TelegramAPI,
        *,
        file_id: str,
        file_name: Optional[str],
        mime_type: Optional[str],
        file_size: Optional[int],
    ) -> str:
        audio_bytes, normalized_name, _ = fetch_telegram_audio(
            api,
            file_id=file_id,
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
            max_bytes=self.max_bytes,
        )
        suffix = Path(normalized_name).suffix or ".ogg"
        model = self._load_model()
        fd, tmp_path = tempfile.mkstemp(prefix="codex-tg-voice-", suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(audio_bytes)
                tmp.flush()
            audio = self._decode_audio(tmp_path)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        try:
            result = model.transcribe(
                audio,
                language=self.language or None,
                fp16=False,
                verbose=False,
            )
        except Exception as e:
            raise RuntimeError(f"本地 Whisper 转写失败: {e}") from e
        text = str((result or {}).get("text") or "").strip()
        if not text:
            raise RuntimeError("本地 Whisper 没有返回文本。")
        return text


class TypingStatus:
    def __init__(self, api: TelegramAPI, chat_id: int, interval_sec: float = 4.0):
        self.api = api
        self.chat_id = chat_id
        self.interval_sec = interval_sec
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.api.send_chat_action(self.chat_id, "typing")
            except Exception:
                pass
            self._stop_event.wait(self.interval_sec)


class TgCodexService:
    def __init__(
        self,
        api: TelegramAPI,
        sessions: SessionStore,
        state: BotState,
        memory_store: MemoryStore,
        codex: CodexRunner,
        audio_transcriber: Optional[AudioTranscriber],
        tts_synthesizer: Optional[LocalGptSovitsTtsSynthesizer],
        default_cwd: Path,
        allowed_user_ids: Optional[Set[int]],
        stream_enabled: bool,
        stream_edit_interval_ms: int,
        stream_min_delta_chars: int,
        thinking_status_interval_ms: int,
        reply_to_messages: bool = False,
        attach_time_context: bool = True,
        user_display_name: str = "对方",
        new_thread_persona_enabled: bool = True,
        new_thread_persona_prompt: str = NEW_THREAD_PERSONA_PROMPT,
        heartbeat_session_prompt: str = HEARTBEAT_SESSION_PROMPT,
        heartbeat_banned_patterns: Optional[List[str]] = None,
        heartbeat_template_messages: Optional[List[str]] = None,
        heartbeat_followup_template_messages: Optional[List[str]] = None,
        memory_context_prompt: Optional[str] = MEMORY_CONTEXT_PROMPT,
        memory_writeback_prompt: Optional[str] = MEMORY_WRITEBACK_PROMPT,
        memory_auto_enabled: bool = True,
        tts_backend: str = "disabled",
        tts_mode: str = "auto",
        tts_max_chars: int = DEFAULT_TTS_MAX_CHARS,
        tts_api_base: str = "",
        tts_default_model: str = "",
        tts_ffmpeg_bin: Optional[str] = None,
        tts_cache_dir: Optional[Path] = None,
    ):
        self.api = api
        self.sessions = sessions
        self.state = state
        self.memory_store = memory_store
        self.codex = codex
        self.audio_transcriber = audio_transcriber
        self.tts_synthesizer = tts_synthesizer
        self.default_cwd = default_cwd
        self.allowed_user_ids = allowed_user_ids
        self.stream_enabled = stream_enabled
        self.stream_edit_interval_ms = max(200, stream_edit_interval_ms)
        self.stream_min_delta_chars = max(1, stream_min_delta_chars)
        self.thinking_status_interval_ms = max(400, thinking_status_interval_ms)
        self.reply_to_messages = bool(reply_to_messages)
        self.attach_time_context = bool(attach_time_context)
        self.user_display_name = (user_display_name or "对方").strip() or "对方"
        self.new_thread_persona_enabled = bool(new_thread_persona_enabled)
        self.new_thread_persona_prompt = (
            NEW_THREAD_PERSONA_PROMPT if new_thread_persona_prompt is None else str(new_thread_persona_prompt)
        ).strip()
        self.heartbeat_session_prompt = (
            HEARTBEAT_SESSION_PROMPT if heartbeat_session_prompt is None else str(heartbeat_session_prompt)
        ).strip()
        self.heartbeat_banned_patterns = (
            [str(item).strip() for item in HEARTBEAT_BANNED_PATTERNS if str(item).strip()]
            if heartbeat_banned_patterns is None
            else [str(item).strip() for item in heartbeat_banned_patterns if str(item).strip()]
        )
        self.heartbeat_template_messages = (
            [str(item).strip() for item in HEARTBEAT_TEMPLATE_MESSAGES if str(item).strip()]
            if heartbeat_template_messages is None
            else [str(item).strip() for item in heartbeat_template_messages if str(item).strip()]
        )
        self.heartbeat_followup_template_messages = (
            [str(item).strip() for item in HEARTBEAT_FOLLOWUP_TEMPLATE_MESSAGES if str(item).strip()]
            if heartbeat_followup_template_messages is None
            else [str(item).strip() for item in heartbeat_followup_template_messages if str(item).strip()]
        )
        self.memory_context_prompt = (
            MEMORY_CONTEXT_PROMPT if memory_context_prompt is None else str(memory_context_prompt)
        ).strip()
        self.memory_writeback_prompt = (
            MEMORY_WRITEBACK_PROMPT if memory_writeback_prompt is None else str(memory_writeback_prompt)
        ).strip()
        self.memory_auto_enabled = bool(memory_auto_enabled)
        self.tts_backend = (tts_backend or "disabled").strip().lower() or "disabled"
        self.tts_mode = (tts_mode or "auto").strip().lower() or "auto"
        self.tts_max_chars = max(40, int(tts_max_chars))
        self.tts_api_base = str(tts_api_base or "").strip()
        self.tts_default_model = str(tts_default_model or "").strip()
        self.tts_ffmpeg_bin = str(tts_ffmpeg_bin or "").strip() or None
        self.tts_cache_dir = Path(tts_cache_dir).expanduser() if tts_cache_dir else None
        if self.tts_cache_dir is not None:
            self.tts_cache_dir.mkdir(parents=True, exist_ok=True)
        self.running_prompts = RunningPromptRegistry()
        self.offset: Optional[int] = None
        self.heartbeat_default_interval_sec = HEARTBEAT_DEFAULT_INTERVAL_SEC
        self.heartbeat_poll_interval_sec = HEARTBEAT_POLL_INTERVAL_SEC
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()

    def run_forever(self) -> None:
        self._ensure_heartbeat_thread()
        while True:
            try:
                updates = self.api.get_updates(self.offset, timeout=30)
                for update in updates:
                    self.offset = update["update_id"] + 1
                    self._handle_update(update)
            except urllib.error.URLError as e:
                print(f"[warn] telegram network error: {e}", file=sys.stderr)
                time.sleep(2)
            except Exception as e:
                print(f"[warn] loop error: {e}", file=sys.stderr)
                traceback.print_exc()
                time.sleep(2)

    def setup_bot_menu(self) -> None:
        self.api.set_my_commands(BOT_COMMANDS)
        try:
            self.api.set_chat_menu_button_commands()
        except Exception:
            # Non-critical; setMyCommands already provides slash-menu commands.
            pass

    def _user_id_for_chat(self, chat_id: int) -> Optional[int]:
        for raw_user_id, user_data in self.state.list_users_snapshot().items():
            if self._coerce_int(user_data.get("last_chat_id")) != int(chat_id):
                continue
            if isinstance(raw_user_id, str) and raw_user_id.isdigit():
                return int(raw_user_id)
        return None

    def _reply_target(self, reply_to: Optional[int]) -> Optional[int]:
        if not self.reply_to_messages:
            return None
        return reply_to

    def _send_message(
        self,
        chat_id: int,
        text: str,
        reply_to: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
        user_id: Optional[int] = None,
    ) -> None:
        self.api.send_message(
            chat_id,
            text,
            reply_to=self._reply_target(reply_to),
            reply_markup=reply_markup,
        )
        target_user_id = user_id if user_id is not None else self._user_id_for_chat(chat_id)
        if target_user_id is not None:
            self.state.touch_assistant(target_user_id, chat_id)

    def _send_message_with_result(
        self,
        chat_id: int,
        text: str,
        reply_to: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        result = self.api.send_message_with_result(
            chat_id,
            text,
            reply_to=self._reply_target(reply_to),
            reply_markup=reply_markup,
        )
        target_user_id = user_id if user_id is not None else self._user_id_for_chat(chat_id)
        if target_user_id is not None:
            self.state.touch_assistant(target_user_id, chat_id)
        return result

    def _send_voice(
        self,
        chat_id: int,
        voice: SynthesizedVoiceNote,
        reply_to: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        result = self.api.send_voice_with_result(
            chat_id=chat_id,
            voice=voice,
            reply_to=self._reply_target(reply_to),
        )
        target_user_id = user_id if user_id is not None else self._user_id_for_chat(chat_id)
        if target_user_id is not None:
            self.state.touch_assistant(target_user_id, chat_id)
        return result

    def _discard_message(self, chat_id: int, message_id: Optional[int]) -> None:
        if message_id is None:
            return
        try:
            self.api.delete_message(chat_id, int(message_id))
        except Exception as e:
            log(f"delete message failed: chat_id={chat_id} message_id={message_id} error={e}")

    @staticmethod
    def _mask_secret(value: str) -> str:
        cleaned = str(value or "").strip()
        if len(cleaned) <= 10:
            return "*" * max(4, len(cleaned))
        return f"{cleaned[:6]}...{cleaned[-4:]}"

    def _tts_settings_for_user(self, user_id: int) -> Dict[str, Any]:
        settings = self.state.get_voice_settings(user_id)
        return settings if isinstance(settings, dict) else {}

    def _tts_status_lines(self, user_id: int) -> List[str]:
        settings = self._tts_settings_for_user(user_id)
        api_key = str(settings.get("api_key") or "").strip()
        voice_id = str(settings.get("voice_id") or "").strip()
        model = str(settings.get("model") or self.tts_default_model or "").strip()
        frequency = _normalize_tts_frequency(settings.get("frequency"))
        if self.tts_backend == "disabled":
            return ["语音回复现在还没开。"]
        lines = [
            "语音回复配置：",
            f"backend: {self.tts_backend}",
            f"API key: {'已设置（' + self._mask_secret(api_key) + '）' if api_key else '未设置'}",
            f"voice_id: {voice_id or '未设置'}",
            f"频率: {TTS_FREQUENCY_LABELS.get(frequency, '中')}",
        ]
        if model:
            lines.append(f"model: {model}")
        lines.append("用法：/voice key <API_KEY> | /voice voice <voice_id> | /voice freq <high|medium|low> | /voice clear")
        return lines

    def _tts_feature_ready_for_user(self, user_id: int) -> bool:
        if self.tts_backend == "local-gpt-sovits":
            return self.tts_synthesizer is not None
        if self.tts_backend == "minimax":
            settings = self._tts_settings_for_user(user_id)
            return bool(str(settings.get("api_key") or "").strip() and str(settings.get("voice_id") or "").strip())
        return False

    def _should_offer_tts_voice(self, user_id: Optional[int], text: str) -> bool:
        if user_id is None:
            return False
        if not self._tts_feature_ready_for_user(int(user_id)):
            return False
        return is_tts_reply_candidate(
            text,
            mode=self.tts_mode,
            max_chars=self.tts_max_chars,
        )

    def _tts_frequency_for_user(self, user_id: Optional[int]) -> str:
        if user_id is None:
            return TTS_FREQUENCY_DEFAULT
        settings = self._tts_settings_for_user(int(user_id))
        return _normalize_tts_frequency(settings.get("frequency"))

    def _build_tts_request_markup(self, user_id: Optional[int], text: str) -> Optional[Dict[str, Any]]:
        if user_id is None or not self._should_offer_tts_voice(int(user_id), text):
            return None
        settings = self._tts_settings_for_user(int(user_id))
        token = self.state.create_tts_request(
            int(user_id),
            text=text,
            voice_id=str(settings.get("voice_id") or "").strip() or None,
            model=str(settings.get("model") or self.tts_default_model or "").strip() or None,
        )
        if not token:
            return None
        return {
            "inline_keyboard": [
                [
                    {
                        "text": TTS_BUTTON_TEXT,
                        "callback_data": f"{TTS_CALLBACK_PREFIX}{token}",
                    }
                ]
            ]
        }

    def _build_user_tts_synthesizer(
        self,
        user_id: int,
        request: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        if self.tts_backend == "local-gpt-sovits":
            return self.tts_synthesizer
        if self.tts_backend != "minimax":
            return None

        settings = self._tts_settings_for_user(user_id)
        api_key = str(settings.get("api_key") or "").strip()
        voice_id = str((request or {}).get("voice_id") or settings.get("voice_id") or "").strip()
        model = str((request or {}).get("model") or settings.get("model") or self.tts_default_model or "").strip()
        if not api_key or not voice_id:
            return None
        synth = MiniMaxTtsSynthesizer(
            api_key=api_key,
            voice_id=voice_id,
            api_base=self.tts_api_base,
            model=model or self.tts_default_model,
            ffmpeg_bin=self.tts_ffmpeg_bin,
            cache_dir=(self.tts_cache_dir / str(user_id)) if self.tts_cache_dir is not None else None,
            max_chars=self.tts_max_chars,
        )
        synth.validate_environment()
        return synth

    def _maybe_offer_conversation_voice(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        _ = reply_to
        return self._build_tts_request_markup(user_id, text)

    def _queue_conversation_voice_reply(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> None:
        if user_id is None:
            return
        normalized_user_id = int(user_id)
        if not self._should_offer_tts_voice(normalized_user_id, text):
            return
        worker = threading.Thread(
            target=self._run_tts_reply_worker,
            args=(chat_id, reply_to, normalized_user_id, text),
            daemon=True,
        )
        worker.start()

    def _split_conversation_paragraph(self, text: str) -> List[str]:
        cleaned = (text or "").strip()
        if not cleaned:
            return []

        line_units = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if len(line_units) > 1:
            flattened: List[str] = []
            for line in line_units:
                flattened.extend(self._split_conversation_paragraph(line))
            return flattened

        sentence_units = [part.strip() for part in re.split(r"(?<=[。！？!?；;…\.])\s*", cleaned) if part.strip()]
        if len(sentence_units) > 1:
            if len(cleaned) <= CONVERSATION_MIN_SPLIT_CHARS and len(sentence_units) <= 2:
                return [cleaned]
            return self._group_conversation_units(sentence_units)

        clause_units = [part.strip() for part in re.split(r"(?<=[，、：:,])\s*", cleaned) if part.strip()]
        if len(clause_units) > 1:
            if len(cleaned) <= CONVERSATION_MIN_SPLIT_CHARS and len(clause_units) <= 3:
                return [cleaned]
            return self._group_conversation_units(clause_units)

        if len(cleaned) <= CONVERSATION_MIN_SPLIT_CHARS:
            return [cleaned]
        return chunk_text(cleaned, size=CONVERSATION_MAX_CHARS)

    def _group_conversation_units(self, units: List[str]) -> List[str]:
        parts: List[str] = []
        current_units: List[str] = []
        current_len = 0

        for unit in units:
            stripped = unit.strip()
            if not stripped:
                continue

            if len(stripped) > CONVERSATION_MAX_CHARS:
                if current_units:
                    parts.append("".join(current_units).strip())
                    current_units = []
                    current_len = 0
                parts.extend(chunk_text(stripped, size=CONVERSATION_MAX_CHARS))
                continue

            candidate_len = current_len + len(stripped)
            should_flush = bool(current_units) and (
                candidate_len > CONVERSATION_MAX_CHARS
                or current_len >= CONVERSATION_TARGET_CHARS
                or len(current_units) >= CONVERSATION_MAX_UNITS_PER_PART
            )
            if should_flush:
                parts.append("".join(current_units).strip())
                current_units = [stripped]
                current_len = len(stripped)
                continue

            current_units.append(stripped)
            current_len = candidate_len

        if current_units:
            parts.append("".join(current_units).strip())
        return [part for part in parts if part]

    def _conversation_blocks(self, text: str) -> List[Tuple[str, bool]]:
        cleaned = (text or "").strip()
        if not cleaned:
            return []

        blocks: List[Tuple[str, bool]] = []
        last_end = 0
        for match in re.finditer(r"```[\s\S]*?```", cleaned):
            prose = cleaned[last_end:match.start()].strip()
            if prose:
                blocks.append((prose, False))
            code_block = match.group(0).strip()
            if code_block:
                for piece in chunk_text(code_block, size=CONVERSATION_CODE_BLOCK_MAX_CHARS):
                    part = piece.strip()
                    if part:
                        blocks.append((part, True))
            last_end = match.end()

        tail = cleaned[last_end:].strip()
        if tail:
            blocks.append((tail, False))
        return blocks or [(cleaned, False)]

    def _conversation_parts(self, text: str) -> List[str]:
        cleaned = (text or "").strip()
        if not cleaned:
            return ["..."]

        parts: List[str] = []

        for block, is_code in self._conversation_blocks(cleaned):
            if is_code:
                parts.append(block)
                continue

            paragraphs = [part.strip() for part in block.split("\n\n") if part.strip()]
            if not paragraphs:
                paragraphs = [block]

            for paragraph in paragraphs:
                parts.extend(self._split_conversation_paragraph(paragraph))

        return [part for part in parts if part] or [cleaned]

    def _voice_delivery_units(self, text: str) -> List[str]:
        cleaned = (text or "").strip()
        if not cleaned:
            return []

        units: List[str] = []
        for block, is_code in self._conversation_blocks(cleaned):
            if is_code:
                units.append(block.strip())
                continue

            paragraphs = [part.strip() for part in re.split(r"\n+", block) if part.strip()]
            if not paragraphs:
                paragraphs = [block.strip()]

            for paragraph in paragraphs:
                sentence_units = [
                    part.strip()
                    for part in re.split(
                        r"(?<=[。！？!?；;…])\s*|(?<=\.)\s+(?=[A-Z0-9\"“‘'])",
                        paragraph,
                    )
                    if part.strip()
                ]
                if not sentence_units:
                    sentence_units = [paragraph.strip()]
                units.extend(sentence_units)

        return [unit for unit in units if unit]

    def _tts_segment_score(self, text: str) -> int:
        cleaned = " ".join((text or "").split()).strip()
        if not cleaned:
            return -999

        score = 0
        length = len(cleaned)
        if length <= 18:
            score += 3
        elif length <= 36:
            score += 2
        elif length <= 80:
            score += 1
        else:
            score -= 1

        if re.search(r"[！？!?~～…]", cleaned):
            score += 1
        if re.search(r"(呀|啦|呢|嘛|喔|哦|哎|诶|嗯|哈|宝|乖|抱抱|想你|过来|别怕)", cleaned):
            score += 2
        if cleaned.endswith(("。", "！", "？", "!", "?", "~", "～", "…")):
            score += 1

        if re.search(r"```|stderr:|Traceback|https?://|[A-Za-z]:\\", cleaned):
            score -= 4
        if re.search(r"^[-*]\s|\d+[.)、]\s", cleaned):
            score -= 2
        if re.fullmatch(r"[0-9A-Za-z_./:\\-]+", cleaned):
            score -= 2

        return score

    def _tts_segment_budget(self, user_id: Optional[int], candidate_count: int) -> int:
        if candidate_count <= 0:
            return 0
        frequency = self._tts_frequency_for_user(user_id)
        if frequency == "low":
            return 1
        if frequency == "high":
            return min(4, candidate_count)
        return min(2, candidate_count)

    def _build_reply_delivery_segments(
        self,
        text: str,
        user_id: Optional[int],
    ) -> List[Tuple[str, bool]]:
        cleaned = (text or "").strip()
        if not cleaned:
            return [("...", False)]
        if user_id is None or not self._tts_feature_ready_for_user(int(user_id)):
            return [(cleaned, False)]

        units = self._voice_delivery_units(cleaned)
        if not units:
            return [(cleaned, False)]

        candidates: List[Tuple[int, int]] = []
        for idx, unit in enumerate(units):
            if not self._should_offer_tts_voice(int(user_id), unit):
                continue
            score = self._tts_segment_score(unit)
            if score < 1:
                continue
            candidates.append((idx, score))

        budget = self._tts_segment_budget(user_id, len(candidates))
        selected_indexes: Set[int] = set()
        if budget > 0 and candidates:
            for idx, _score in sorted(candidates, key=lambda item: (-item[1], item[0]))[:budget]:
                selected_indexes.add(idx)

        segments: List[Tuple[str, bool]] = []
        current_text = ""
        current_is_voice: Optional[bool] = None
        for idx, unit in enumerate(units):
            is_voice = idx in selected_indexes
            if current_is_voice is None:
                current_text = unit
                current_is_voice = is_voice
                continue
            if current_is_voice == is_voice:
                current_text += unit
                continue
            if current_text.strip():
                segments.append((current_text.strip(), bool(current_is_voice)))
            current_text = unit
            current_is_voice = is_voice

        if current_text.strip():
            segments.append((current_text.strip(), bool(current_is_voice)))
        return segments or [(cleaned, False)]

    def _send_conversation_message(
        self,
        chat_id: int,
        text: str,
        reply_to: Optional[int] = None,
        user_id: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        parts = self._conversation_parts(text)
        log(
            "conversation send: "
            f"chat_id={chat_id} parts={len(parts)} lens={[len(part) for part in parts[:8]]}"
        )
        for idx, part in enumerate(parts):
            self._send_message(
                chat_id,
                part,
                reply_to=reply_to if idx == 0 else None,
                user_id=user_id,
                reply_markup=reply_markup if idx == len(parts) - 1 else None,
            )
            if idx < len(parts) - 1:
                time.sleep(CONVERSATION_PART_DELAY_SEC)

    def _send_delivery_segments(
        self,
        chat_id: int,
        reply_to: Optional[int],
        user_id: int,
        segments: List[Tuple[str, bool]],
        *,
        stream_message_id: Optional[int] = None,
        progressive_replay: bool = False,
    ) -> None:
        normalized_segments = [(str(text or "").strip(), bool(is_voice)) for text, is_voice in segments if str(text or "").strip()]
        if not normalized_segments:
            normalized_segments = [("Codex 没有返回可展示内容。", False)]

        log(
            "delivery segments: "
            f"chat_id={chat_id} segments={[(len(text), 'voice' if is_voice else 'text') for text, is_voice in normalized_segments[:8]]}"
        )

        pending_reply_to = reply_to
        remaining = list(normalized_segments)
        if stream_message_id is not None:
            first_text = next(((idx, text) for idx, (text, is_voice) in enumerate(remaining) if not is_voice), None)
            if first_text is not None and first_text[0] == 0:
                first_text_value = first_text[1]
                try:
                    self.api.edit_message_text(chat_id, stream_message_id, first_text_value)
                    pending_reply_to = None
                    remaining = remaining[1:]
                    if progressive_replay and not remaining and len(first_text_value) > 240:
                        full = first_text_value
                        step = 120
                        interval_sec = 0.12
                        for end in range(step, len(full), step):
                            partial = full[:end].rstrip()
                            if not partial:
                                continue
                            preview = f"{partial}\n\n[生成中...]"
                            try:
                                self.api.edit_message_text(chat_id, stream_message_id, preview)
                            except Exception:
                                break
                            time.sleep(interval_sec)
                        try:
                            self.api.edit_message_text(chat_id, stream_message_id, full)
                        except Exception:
                            pass
                except Exception as e:
                    log(f"stream final edit failed: {e}")
                    self._discard_message(chat_id, stream_message_id)
            else:
                self._discard_message(chat_id, stream_message_id)

        for idx, (segment_text, is_voice) in enumerate(remaining):
            current_reply_to = pending_reply_to if idx == 0 else None
            if is_voice:
                voice_sent = self._deliver_tts_voice(
                    chat_id,
                    reply_to=current_reply_to,
                    user_id=user_id,
                    text=segment_text,
                    request=None,
                    notify_errors=False,
                )
                if not voice_sent:
                    self._send_conversation_message(
                        chat_id,
                        segment_text,
                        reply_to=current_reply_to,
                        user_id=user_id,
                    )
            else:
                self._send_conversation_message(
                    chat_id,
                    segment_text,
                    reply_to=current_reply_to,
                    user_id=user_id,
                )
            pending_reply_to = None

    def _ensure_heartbeat_thread(self) -> None:
        if self._heartbeat_thread is not None:
            return
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.is_set():
            try:
                self._run_due_heartbeats_once()
            except Exception as e:
                log(f"heartbeat loop error: {e}")
            self._heartbeat_stop.wait(self.heartbeat_poll_interval_sec)

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _heartbeat_interval_sec(self, heartbeat: Dict[str, Any]) -> int:
        value = self._coerce_int(heartbeat.get("interval_sec"))
        if value is None or value <= 0:
            value = self.heartbeat_default_interval_sec
        return max(HEARTBEAT_MIN_INTERVAL_SEC, value)

    @staticmethod
    def _format_elapsed(seconds: Optional[int]) -> str:
        if seconds is None:
            return "未记录"
        if seconds < 10:
            return "刚刚"
        if seconds < 60:
            return f"{seconds} 秒前"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} 分钟前"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} 小时前"
        days = hours // 24
        return f"{days} 天前"

    @staticmethod
    def _tokyo_datetime(timestamp: int) -> datetime:
        return datetime.fromtimestamp(timestamp, TOKYO_TZ)

    def _format_message_context_time(self, timestamp: Optional[int]) -> str:
        resolved_ts = int(timestamp if timestamp is not None else time.time())
        return self._tokyo_datetime(resolved_ts).strftime("%Y-%m-%d %H:%M")

    def _decorate_text_prompt_with_context(self, text: str, message_ts: Optional[int]) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            return cleaned
        if not self.attach_time_context:
            return cleaned
        return (
            f"当前时间：{self._format_message_context_time(message_ts)} (Asia/Tokyo)\n"
            f"{self.user_display_name}说：{cleaned}"
        )

    def _decorate_audio_prompt_with_context(
        self,
        transcript: str,
        caption: str,
        message_ts: Optional[int],
    ) -> str:
        transcript_text = (transcript or "").strip()
        caption_text = (caption or "").strip()
        if not self.attach_time_context:
            if caption_text:
                return f"附加说明:\n{caption_text}\n\n语音转写:\n{transcript_text}"
            return transcript_text

        lines = [f"当前时间：{self._format_message_context_time(message_ts)} (Asia/Tokyo)"]
        if caption_text:
            lines.append(f"{self.user_display_name}补充说：{caption_text}")
        lines.append(f"{self.user_display_name}的语音转写：{transcript_text}")
        return "\n".join(lines)

    def _decorate_attachment_prompt_with_context(
        self,
        attachment: TelegramAttachment,
        caption: str,
        message_ts: Optional[int],
    ) -> str:
        caption_text = (caption or "").strip()
        intro = "发来了一张图片" if attachment.is_image else "发来一个文件"
        followup = (
            "请结合随附图片一起理解并回应；如果有需要，也可以读取这个路径里的文件。"
            if attachment.is_image
            else "请先读取这个文件，再结合她的说明继续回应。"
        )
        lines: List[str] = []
        if self.attach_time_context:
            lines.append(f"当前时间：{self._format_message_context_time(message_ts)} (Asia/Tokyo)")
        lines.append(f"{self.user_display_name}{intro}。")
        lines.append(f"文件路径：{attachment.local_path}")
        lines.append(f"文件名：{attachment.display_name}")
        lines.append(f"MIME 类型：{attachment.mime_type}")
        if caption_text:
            lines.append(f"{self.user_display_name}补充说：{caption_text}")
        lines.append(followup)
        return "\n".join(lines)

    def _select_photo_media(self, value: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(value, list):
            return None
        candidates = [item for item in value if isinstance(item, dict) and item.get("file_id")]
        if not candidates:
            return None

        def sort_key(item: Dict[str, Any]) -> Tuple[int, int, int]:
            return (
                self._coerce_int(item.get("file_size")) or -1,
                self._coerce_int(item.get("width")) or -1,
                self._coerce_int(item.get("height")) or -1,
            )

        return max(candidates, key=sort_key)

    @staticmethod
    def _attachment_storage_dir(cwd: Path) -> Path:
        target = cwd / ".codex-tg-attachments"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _write_telegram_attachment(
        self,
        cwd: Path,
        media: Dict[str, Any],
        *,
        kind: str,
    ) -> TelegramAttachment:
        file_id = str(media.get("file_id") or "").strip()
        if not file_id:
            raise RuntimeError("无法读取这条附件消息的文件 ID。")

        file_name = media.get("file_name")
        mime_type_raw = media.get("mime_type")
        mime_type = str(mime_type_raw).strip() if mime_type_raw else None
        file_size_raw = media.get("file_size")
        file_size = file_size_raw if isinstance(file_size_raw, int) else None

        default_stem = "telegram-photo" if kind == "photo" else "telegram-file"
        default_suffix = ".jpg" if kind == "photo" else ""
        if kind == "photo" and not mime_type:
            mime_type = "image/jpeg"

        file_bytes, normalized_name, content_type, _ = fetch_telegram_file(
            self.api,
            file_id=file_id,
            file_name=str(file_name).strip() if file_name else None,
            mime_type=mime_type,
            file_size=file_size,
            max_bytes=MAX_TELEGRAM_ATTACHMENT_BYTES,
            default_stem=default_stem,
            default_suffix=default_suffix,
        )
        saved_name = f"{int(time.time())}-{uuid.uuid4().hex[:8]}-{normalized_name}"
        target_path = self._attachment_storage_dir(cwd) / saved_name
        target_path.write_bytes(file_bytes)
        resolved_path = target_path.resolve()
        return TelegramAttachment(
            local_path=resolved_path,
            display_name=normalized_name,
            mime_type=content_type,
            size_bytes=len(file_bytes),
            is_image=content_type.startswith("image/"),
            kind=kind,
        )

    @staticmethod
    def _normalize_memory_category(value: Any) -> str:
        normalized = str(value or "").strip().lower() or "general"
        return normalized if normalized in MEMORY_ALLOWED_CATEGORIES else "general"

    @staticmethod
    def _normalize_memory_source_text(text: Optional[str]) -> str:
        cleaned = " ".join(str(text or "").split()).strip()
        if len(cleaned) > MEMORY_WRITEBACK_MAX_INPUT_CHARS:
            cleaned = cleaned[:MEMORY_WRITEBACK_MAX_INPUT_CHARS].rstrip()
        return cleaned

    def _humanize_memory_text(self, text: str) -> str:
        cleaned = " ".join(str(text or "").split()).strip()
        if not cleaned:
            return ""
        display_name = self.user_display_name or "对方"
        cleaned = cleaned.replace("用户", display_name).replace("对方", display_name)
        cleaned = cleaned.replace("会称呼我", "会叫我").replace("称呼我", "叫我")
        alias_match = re.match(rf'^{re.escape(display_name)}会叫我["“]?(.+?)["”]?[。！!]*$', cleaned)
        if alias_match:
            cleaned = f'{display_name}会叫我“{alias_match.group(1)}”。'
        birthday_match = re.match(rf"^{re.escape(display_name)}出生于(.+?)，生日是(.+?)[。！!]*$", cleaned)
        if birthday_match:
            birth_text, birthday_text = birthday_match.groups()
            birthday_text = birthday_text.strip()
            if birthday_text and birthday_text in birth_text:
                cleaned = f"{display_name}出生于{birth_text}。"
            else:
                cleaned = f"{display_name}出生于{birth_text}，生日是{birthday_text}。"
        if not re.search(r"[。！？!?]$", cleaned):
            cleaned += "。"
        return cleaned

    def _memory_category_label(self, value: Any) -> str:
        category = self._normalize_memory_category(value)
        return MEMORY_CATEGORY_LABELS.get(category, MEMORY_CATEGORY_LABELS["general"])

    def _select_memories_for_prompt(
        self,
        user_id: int,
        memory_query_text: Optional[str],
        active_id: Optional[str],
    ) -> List[Dict[str, Any]]:
        selected: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        query_text = self._normalize_memory_source_text(memory_query_text)[:MEMORY_SEARCH_QUERY_MAX_CHARS]
        all_memories = self.memory_store.list_memories(user_id, limit=MEMORY_MAX_LIST_ITEMS)
        pinned = [item for item in all_memories if item.get("pinned")]
        relevant = self.memory_store.search_memories(user_id, query_text, limit=MEMORY_MAX_PROMPT_ITEMS) if query_text else []

        def add_items(items: List[Dict[str, Any]]) -> None:
            for item in items:
                memory_id = str(item.get("id") or "").strip()
                if not memory_id or memory_id in seen_ids:
                    continue
                seen_ids.add(memory_id)
                selected.append(item)
                if len(selected) >= MEMORY_MAX_PROMPT_ITEMS:
                    return

        add_items(pinned)
        if len(selected) < MEMORY_MAX_PROMPT_ITEMS:
            add_items(relevant)
        if active_id is None and len(selected) < MEMORY_MAX_PROMPT_ITEMS:
            recent = [item for item in all_memories if not item.get("pinned")][:MEMORY_MAX_RECENT_ITEMS]
            add_items(recent)
        return selected[:MEMORY_MAX_PROMPT_ITEMS]

    def _decorate_prompt_with_memory_context(
        self,
        user_id: int,
        prompt: str,
        memory_query_text: Optional[str],
        active_id: Optional[str],
    ) -> str:
        cleaned = (prompt or "").strip()
        if not cleaned:
            return cleaned
        if not self.memory_context_prompt:
            return cleaned
        memories = self._select_memories_for_prompt(user_id, memory_query_text, active_id)
        if not memories:
            return cleaned
        header = _render_prompt_template(
            self.memory_context_prompt,
            USER_DISPLAY_NAME=self.user_display_name,
        )
        if not header:
            return cleaned
        lines = [line for line in header.splitlines() if line.strip()]
        for item in memories:
            category_label = self._memory_category_label(item.get("category"))
            pinned_prefix = "置顶/" if item.get("pinned") else ""
            lines.append(f"- [{item.get('id')}] {pinned_prefix}{category_label}：{item.get('text')}")
        return "\n".join(lines) + "\n\n" + cleaned

    def _build_memory_writeback_prompt(self, user_id: int, source_text: str) -> str:
        if not self.memory_writeback_prompt:
            return ""
        recent_memories = self.memory_store.list_memories(user_id, limit=8)
        existing_lines = [
            f"- {item.get('text')}"
            for item in recent_memories
            if str(item.get("text") or "").strip()
        ]
        existing_text = "\n".join(existing_lines) if existing_lines else "- 暂无"
        return _render_prompt_template(
            self.memory_writeback_prompt,
            USER_DISPLAY_NAME=self.user_display_name,
            EXISTING_MEMORIES=existing_text,
            SOURCE_TEXT=source_text,
        )

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        cleaned = (text or "").strip()
        if cleaned.startswith("```") and cleaned.endswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned.strip()

    def _parse_memory_writeback_response(self, text: str) -> List[Dict[str, Any]]:
        cleaned = self._strip_code_fence(text)
        parsed: Optional[Dict[str, Any]] = None
        try:
            maybe = json.loads(cleaned)
            if isinstance(maybe, dict):
                parsed = maybe
        except Exception:
            match = re.search(r"\{[\s\S]*\}", cleaned)
            if match:
                try:
                    maybe = json.loads(match.group(0))
                    if isinstance(maybe, dict):
                        parsed = maybe
                except Exception:
                    parsed = None
        if not parsed or not parsed.get("save"):
            return []
        raw_memories = parsed.get("memories")
        if not isinstance(raw_memories, list):
            return []
        results: List[Dict[str, Any]] = []
        seen_texts: Set[str] = set()
        for item in raw_memories:
            if not isinstance(item, dict):
                continue
            text_value = self._humanize_memory_text(self._normalize_memory_source_text(item.get("text")))
            if not text_value or text_value in seen_texts:
                continue
            seen_texts.add(text_value)
            tags_raw = item.get("tags")
            tags = [str(tag).strip() for tag in tags_raw if str(tag).strip()] if isinstance(tags_raw, list) else []
            results.append(
                {
                    "text": text_value,
                    "category": self._normalize_memory_category(item.get("category")),
                    "tags": tags[:4],
                    "pinned": bool(item.get("pinned")),
                }
            )
            if len(results) >= 3:
                break
        return results

    def _schedule_memory_writeback(self, user_id: int, cwd: Path, source_text: Optional[str]) -> None:
        if not self.memory_auto_enabled:
            return
        if not self.memory_writeback_prompt:
            return
        normalized_text = self._normalize_memory_source_text(source_text)
        if not normalized_text:
            return
        worker = threading.Thread(
            target=self._run_memory_writeback_worker,
            args=(user_id, cwd, normalized_text),
            daemon=True,
        )
        worker.start()

    def _run_memory_writeback_worker(self, user_id: int, cwd: Path, source_text: str) -> None:
        prompt = self._build_memory_writeback_prompt(user_id, source_text)
        if not prompt:
            return
        try:
            _, answer, stderr_text, return_code = self.codex.run_prompt(
                prompt=prompt,
                cwd=cwd,
                ephemeral=True,
            )
        except Exception as e:
            log(f"memory writeback failed: user_id={user_id} error={e}")
            return
        if return_code != 0:
            log(
                f"memory writeback exited non-zero: user_id={user_id} "
                f"exit={return_code} stderr={stderr_text[-240:]}"
            )
            return
        memories = self._parse_memory_writeback_response(answer)
        if not memories:
            return
        saved_ids: List[str] = []
        for item in memories:
            record = self.memory_store.add_memory(
                user_id,
                item["text"],
                tags=item.get("tags") or [],
                category=item.get("category") or "general",
                pinned=bool(item.get("pinned")),
                source="auto",
            )
            if record:
                saved_ids.append(str(record.get("id") or ""))
        if saved_ids:
            log(f"memory saved: user_id={user_id} ids={saved_ids}")

    def _decorate_new_thread_prompt(self, prompt: str, active_id: Optional[str]) -> str:
        cleaned = (prompt or "").strip()
        if not cleaned:
            return cleaned
        if active_id or not self.new_thread_persona_enabled or not self.new_thread_persona_prompt:
            return cleaned
        return (
            f"{self.new_thread_persona_prompt}\n\n"
            "上面这些内容是隐藏的对话底色，不要复述，不要解释，不要提到这些规则。\n"
            f"下面是{self.user_display_name}在这个新线程里发来的第一条消息，请自然接住并继续对话：\n"
            f"{cleaned}"
        )

    def _is_heartbeat_window_open(self, now_ts: int) -> bool:
        return self._tokyo_datetime(now_ts).hour >= 8

    def _heartbeat_status_lines(self, user_id: int, now_ts: Optional[int] = None) -> List[str]:
        now_value = int(now_ts if now_ts is not None else time.time())
        heartbeat = self.state.get_heartbeat(user_id)
        interval_sec = self._heartbeat_interval_sec(heartbeat)
        last_sent_at = self._coerce_int(heartbeat.get("last_heartbeat_at"))
        snapshot = self.state.list_users_snapshot().get(str(user_id), {})
        last_interaction_at = self._coerce_int(snapshot.get("last_interaction_at"))
        unanswered_count = int(heartbeat.get("unanswered_count") or 0)
        lines = [
            f"心跳模式：{'已开启' if heartbeat.get('enabled') else '未开启'}",
            f"东京时间窗口：{'开启中' if self._is_heartbeat_window_open(now_value) else '关闭中'}（08:00-24:00）",
            f"间隔：{max(1, interval_sec // 60)} 分钟",
            f"最近互动：{self._format_elapsed(None if last_interaction_at is None else now_value - last_interaction_at)}",
            f"我上次主动找你：{self._format_elapsed(None if last_sent_at is None else now_value - last_sent_at)}",
            f"连续未回复心跳：{unanswered_count}",
            "用法：/heartbeat on 30 | /heartbeat off | /heartbeat now",
        ]
        return lines

    def _resolve_heartbeat_context(self, user_id: int) -> Tuple[Optional[str], Path]:
        active_id, active_cwd = self.state.get_active(user_id)
        context_id, context_cwd = self.state.get_heartbeat_context(user_id)
        session_id = active_id or context_id
        cwd_raw = active_cwd or context_cwd or str(self.default_cwd)
        cwd = Path(cwd_raw).expanduser()
        if not cwd.exists():
            cwd = self.default_cwd
        if session_id and not self.sessions.find_by_id(session_id):
            session_id = None
        return session_id, cwd

    def _build_heartbeat_prompt(
        self,
        *,
        now_ts: int,
        heartbeat: Dict[str, Any],
        last_user_at: Optional[int],
        force: bool = False,
    ) -> str:
        if not self.heartbeat_session_prompt:
            return ""
        idle_minutes = max(0, int((now_ts - (last_user_at or now_ts)) // 60))
        local_now = self._tokyo_datetime(now_ts).strftime("%Y-%m-%d %H:%M")
        lines = [
            self.heartbeat_session_prompt,
            f"当前东京时间：{local_now}",
            f"距离用户上次发消息约：{idle_minutes} 分钟",
        ]
        banned_patterns = "、".join(f"“{pattern}”" for pattern in self.heartbeat_banned_patterns)
        if banned_patterns:
            lines.append(f"尽量避免这些泛泛开场或固定句式：{banned_patterns}。除非当前上下文真的很适合。")
        if force:
            lines.append("这是用户刚刚手动触发的一次主动消息请求，直接发消息，不要输出 SKIP。")
            return "\n".join(lines)

        unanswered_count = int(heartbeat.get("unanswered_count") or 0)
        if unanswered_count <= 0:
            lines.append("请顺着当前会话内容和这个时间点，自然地来碰她一下。")
            return "\n".join(lines)

        last_heartbeat_at = self._coerce_int(heartbeat.get("last_heartbeat_at"))
        since_last_heartbeat = max(0, int((now_ts - last_heartbeat_at) // 60)) if last_heartbeat_at else 0
        lines.extend(
            [
                "你在负责判断这次要不要继续主动发消息。",
                "如果你判断现在不该继续发，只输出：SKIP",
                f"距离上次主动消息约：{since_last_heartbeat} 分钟",
                f"连续未回复的主动消息次数：{unanswered_count}",
                "如果已经连续几次没回，可以更明显一点地问近况，但不要咄咄逼人，也不要反复说同一种话。",
            ]
        )
        return "\n".join(lines)

    def _run_due_heartbeats_once(self, now_ts: Optional[int] = None) -> None:
        current = int(now_ts if now_ts is not None else time.time())
        for raw_user_id, user_data in self.state.list_users_snapshot().items():
            heartbeat = user_data.get("heartbeat")
            if not isinstance(heartbeat, dict) or not heartbeat.get("enabled"):
                continue
            chat_id = self._coerce_int(user_data.get("last_chat_id"))
            last_interaction_at = self._coerce_int(user_data.get("last_interaction_at"))
            if chat_id is None or last_interaction_at is None:
                continue
            interval_sec = self._heartbeat_interval_sec(heartbeat)
            if not self._is_heartbeat_window_open(current):
                continue
            not_before_at = self._coerce_int(heartbeat.get("not_before_at")) or 0
            if current < not_before_at:
                continue
            if current - last_interaction_at < HEARTBEAT_CONVERSATION_COOLDOWN_SEC:
                continue
            if current - last_interaction_at < interval_sec:
                continue
            unanswered_count = int(heartbeat.get("unanswered_count") or 0)
            if unanswered_count >= HEARTBEAT_MAX_UNANSWERED:
                continue
            user_id: Any = raw_user_id
            if isinstance(raw_user_id, str) and raw_user_id.isdigit():
                user_id = int(raw_user_id)
            self._trigger_heartbeat(chat_id=chat_id, user_id=user_id, force=False)

    def _render_template_heartbeat(self, unanswered_count: int = 0) -> str:
        if unanswered_count <= 0:
            if not self.heartbeat_template_messages:
                return ""
            return random.choice(self.heartbeat_template_messages)
        if not self.heartbeat_followup_template_messages:
            return ""
        return random.choice(self.heartbeat_followup_template_messages)

    def _run_heartbeat_worker(
        self,
        chat_id: int,
        user_id: int,
        context_session_id: str,
        cwd: Path,
        heartbeat: Dict[str, Any],
        interval_sec: int,
        force: bool,
    ) -> None:
        current_snapshot = self.state.list_users_snapshot().get(str(user_id), {})
        last_user_at = self._coerce_int(current_snapshot.get("last_user_message_at"))
        prompt = self._build_heartbeat_prompt(
            now_ts=int(time.time()),
            heartbeat=heartbeat,
            last_user_at=last_user_at,
            force=force,
        )
        if not prompt:
            self.state.mark_heartbeat_skipped(user_id)
            self.state.set_heartbeat_not_before(user_id, int(time.time()) + interval_sec)
            self.running_prompts.finish(user_id, context_session_id)
            return
        try:
            thread_id, answer, stderr_text, return_code = self.codex.run_prompt(
                prompt=prompt,
                cwd=cwd,
                session_id=context_session_id,
            )
        except Exception as e:
            log(f"heartbeat prompt failed: user_id={user_id} session={context_session_id} error={e}")
            self.state.mark_heartbeat_skipped(user_id)
            self.state.set_heartbeat_not_before(user_id, int(time.time()) + interval_sec)
            self.running_prompts.finish(user_id, context_session_id)
            return

        self.running_prompts.finish(user_id, context_session_id)
        final_session_id = thread_id or context_session_id
        if final_session_id:
            self.sessions.mark_as_desktop_session(final_session_id)
            self.state.set_heartbeat_context(user_id, final_session_id, str(cwd))

        if return_code != 0:
            log(
                f"heartbeat prompt exit={return_code} user_id={user_id} "
                f"session={context_session_id} stderr={stderr_text[-240:]}"
            )
            self.state.mark_heartbeat_skipped(user_id)
            self.state.set_heartbeat_not_before(user_id, int(time.time()) + interval_sec)
            return

        text = (answer or "").strip()
        if text.upper() == "SKIP":
            self.state.mark_heartbeat_skipped(user_id)
            self.state.set_heartbeat_not_before(user_id, int(time.time()) + interval_sec)
            return

        if not text:
            self.state.mark_heartbeat_skipped(user_id)
            self.state.set_heartbeat_not_before(user_id, int(time.time()) + interval_sec)
            return

        self._send_conversation_message(chat_id, text, user_id=user_id)
        self.state.mark_heartbeat_sent(
            user_id,
            chat_id,
            session_id=final_session_id,
            cwd=str(cwd),
        )
        self.state.set_heartbeat_not_before(user_id, int(time.time()) + interval_sec)

    def _trigger_heartbeat(self, chat_id: int, user_id: int, force: bool) -> bool:
        heartbeat = self.state.get_heartbeat(user_id)
        interval_sec = self._heartbeat_interval_sec(heartbeat)
        context_session_id, cwd = self._resolve_heartbeat_context(user_id)

        if context_session_id:
            if not self.running_prompts.try_start(user_id, context_session_id):
                return False
            worker = threading.Thread(
                target=self._run_heartbeat_worker,
                args=(chat_id, user_id, context_session_id, cwd, heartbeat, interval_sec, force),
                daemon=True,
            )
            try:
                worker.start()
            except Exception:
                self.running_prompts.finish(user_id, context_session_id)
                raise
            return True

        text = self._render_template_heartbeat(int(heartbeat.get("unanswered_count") or 0))
        if not text:
            self.state.mark_heartbeat_skipped(user_id)
            self.state.set_heartbeat_not_before(user_id, int(time.time()) + interval_sec)
            return False
        self._send_conversation_message(chat_id, text, user_id=user_id)
        self.state.mark_heartbeat_sent(user_id, chat_id, cwd=str(cwd))
        self.state.set_heartbeat_not_before(user_id, int(time.time()) + interval_sec)
        return True

    def _handle_update(self, update: Dict[str, Any]) -> None:
        callback_query = update.get("callback_query")
        if callback_query:
            self._handle_callback_query(callback_query)
            return

        msg = update.get("message")
        if not msg:
            return
        text = (msg.get("text") or "").strip()
        caption = (msg.get("caption") or "").strip()
        voice = msg.get("voice") if isinstance(msg.get("voice"), dict) else None
        audio = msg.get("audio") if isinstance(msg.get("audio"), dict) else None
        photo = self._select_photo_media(msg.get("photo"))
        document = msg.get("document") if isinstance(msg.get("document"), dict) else None

        chat_id = msg["chat"]["id"]
        message_id = msg["message_id"]
        message_ts = self._coerce_int(msg.get("date"))
        user = msg.get("from") or {}
        user_id = user.get("id")

        if user_id is None:
            return
        message_preview = text or caption
        log(
            f"update received: user_id={user_id} chat_id={chat_id} "
            f"text={message_preview[:80]!r} voice={bool(voice)} audio={bool(audio)} "
            f"photo={bool(photo)} document={bool(document)}"
        )

        if self.allowed_user_ids is not None and int(user_id) not in self.allowed_user_ids:
            log(f"blocked by allowlist: user_id={user_id}")
            self._send_message(chat_id, "没有权限使用这个 bot。", reply_to=message_id)
            return

        self.state.touch_user(int(user_id), int(chat_id))

        if not text:
            if voice:
                self._handle_audio_message(
                    chat_id=chat_id,
                    reply_to=message_id,
                    user_id=int(user_id),
                    media=voice,
                    caption=caption,
                    kind="voice",
                    message_ts=message_ts,
                )
            elif audio:
                self._handle_audio_message(
                    chat_id=chat_id,
                    reply_to=message_id,
                    user_id=int(user_id),
                    media=audio,
                    caption=caption,
                    kind="audio",
                    message_ts=message_ts,
                )
            elif photo:
                self._handle_attachment_message(
                    chat_id=chat_id,
                    reply_to=message_id,
                    user_id=int(user_id),
                    media=photo,
                    caption=caption,
                    kind="photo",
                    message_ts=message_ts,
                )
            elif document:
                self._handle_attachment_message(
                    chat_id=chat_id,
                    reply_to=message_id,
                    user_id=int(user_id),
                    media=document,
                    caption=caption,
                    kind="document",
                    message_ts=message_ts,
                )
            return
        if not text.startswith("/"):
            if self._try_handle_quick_session_pick(chat_id, message_id, int(user_id), text):
                return
            self.state.set_pending_session_pick(int(user_id), False)
            self._handle_chat_message(chat_id, message_id, int(user_id), text, message_ts=message_ts)
            return

        cmd, arg = self._parse_command(text)
        log(f"command: /{cmd} arg={arg[:80]!r}")
        if cmd in ("start", "help"):
            self._send_help(chat_id, message_id)
            return
        if cmd == "sessions":
            self._handle_sessions(chat_id, message_id, arg, int(user_id))
            return
        if cmd == "use":
            self._handle_use(chat_id, message_id, int(user_id), arg)
            return
        if cmd == "status":
            self._handle_status(chat_id, message_id, int(user_id))
            return
        if cmd == "new":
            self._handle_new(chat_id, message_id, int(user_id), arg)
            return
        if cmd == "history":
            self._handle_history(chat_id, message_id, int(user_id), arg)
            return
        if cmd == "ask":
            self._handle_ask(chat_id, message_id, int(user_id), arg, message_ts=message_ts)
            return
        if cmd == "heartbeat":
            self._handle_heartbeat(chat_id, message_id, int(user_id), arg)
            return
        if cmd == "memory":
            self._handle_memory(chat_id, message_id, int(user_id), arg)
            return
        if cmd == "voice":
            self._handle_voice(chat_id, message_id, int(user_id), arg)
            return

        self._send_message(chat_id, f"未知命令: /{cmd}\n发送 /help 查看说明。", reply_to=message_id)

    def _handle_callback_query(self, callback_query: Dict[str, Any]) -> None:
        cq_id = callback_query.get("id")
        data = (callback_query.get("data") or "").strip()
        msg = callback_query.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        reply_to = msg.get("message_id")
        user = callback_query.get("from") or {}
        user_id = user.get("id")

        if not cq_id or user_id is None:
            return
        if self.allowed_user_ids is not None and int(user_id) not in self.allowed_user_ids:
            self.api.answer_callback_query(cq_id, text="没有权限。", show_alert=True)
            return
        if not isinstance(chat_id, int):
            self.api.answer_callback_query(cq_id, text="无法解析聊天上下文。", show_alert=True)
            return

        if data.startswith("use:"):
            session_id = data[4:]
            self.api.answer_callback_query(cq_id, text="正在切换会话...")
            self._switch_to_session(chat_id, reply_to, int(user_id), session_id)
            return

        if data.startswith(TTS_CALLBACK_PREFIX):
            token = data[len(TTS_CALLBACK_PREFIX) :].strip()
            request = self.state.get_tts_request(int(user_id), token)
            if not request:
                self.api.answer_callback_query(cq_id, text="这条语音入口已经失效了。", show_alert=True)
                return
            try:
                synth = self._build_user_tts_synthesizer(int(user_id), request)
            except Exception as e:
                self.api.answer_callback_query(cq_id, text=f"语音配置不可用：{e}", show_alert=True)
                return
            if synth is None:
                self.api.answer_callback_query(cq_id, text="你还没配好语音 key 或 voice_id。", show_alert=True)
                return
            self.api.answer_callback_query(cq_id, text="好，我现在开口。")
            worker = threading.Thread(
                target=self._run_tts_callback_worker,
                args=(chat_id, reply_to, int(user_id), token),
                daemon=True,
            )
            worker.start()
            return

        self.api.answer_callback_query(cq_id, text="不支持的操作。", show_alert=True)

    @staticmethod
    def _parse_command(text: str) -> Tuple[str, str]:
        parts = text.split(" ", 1)
        cmd = parts[0][1:]
        cmd = cmd.split("@", 1)[0].strip().lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        return cmd, arg

    def _send_help(self, chat_id: int, reply_to: int) -> None:
        self._send_message(
            chat_id,
            "\n".join(
                [
                    "可用命令:",
                    "/sessions [N] - 查看最近 N 条会话（标题 + 编号）",
                    "/use <编号|session_id> - 切换当前会话",
                    "/history [编号|session_id] [N] - 查看会话最近 N 条消息",
                    "/new [cwd] - 进入新会话模式（下一条普通消息会新建 session）",
                    "/status - 查看当前绑定会话",
                    "/ask <内容> - 手动提问（可选）",
                    "/heartbeat [status|on N|off|now] - 开关主动心跳",
                    "/voice - 配置语音 key、voice_id 和语音频率",
                    "执行 /sessions 后，可直接发送编号切换会话",
                    "执行 /sessions 后，也可点击按钮直接切换会话",
                    "后台执行时仍可发送 /use /sessions /status",
                    "直接发普通消息即可对话（会自动续聊当前 session）",
                    "已配置转写时，也可直接发送 Telegram 语音或音频消息",
                ]
            ),
            reply_to=reply_to,
        )

    def _handle_sessions(self, chat_id: int, reply_to: int, arg: str, user_id: int) -> None:
        limit = 10
        if arg:
            try:
                limit = max(1, min(30, int(arg)))
            except ValueError:
                self._send_message(chat_id, "参数错误，示例: /sessions 10", reply_to=reply_to)
                return
        items = self.sessions.list_recent(limit=limit)
        if not items:
            self._send_message(chat_id, "未找到本地会话记录。", reply_to=reply_to)
            return
        lines = ["最近会话（用 /use 编号 切换）:"]
        session_ids = [s.session_id for s in items]
        keyboard_rows: List[List[Dict[str, str]]] = []
        for i, s in enumerate(items, start=1):
            short_id = s.session_id[:8]
            cwd_name = Path(s.cwd).name or s.cwd
            lines.append(f"{i}. {s.title} | {short_id} | {cwd_name}")
            keyboard_rows.append(
                [
                    {
                        "text": f"切换 {i}",
                        "callback_data": f"use:{s.session_id}",
                    }
                ]
            )
        lines.append("直接发送编号即可切换（例如发送: 1）")
        self._send_message(
            chat_id,
            "\n".join(lines),
            reply_to=reply_to,
            reply_markup={"inline_keyboard": keyboard_rows},
        )
        self.state.set_last_session_ids(user_id, session_ids)
        self.state.set_pending_session_pick(user_id, True)

    def _handle_use(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        selector = arg.strip()
        if not selector:
            self._send_message(chat_id, "示例: /use 1 或 /use <session_id>", reply_to=reply_to)
            return
        session_id, err = self._resolve_session_selector(user_id, selector)
        if err:
            self._send_message(chat_id, err, reply_to=reply_to)
            return
        if not session_id:
            self._send_message(chat_id, "无效的会话选择参数。", reply_to=reply_to)
            return
        self._switch_to_session(chat_id, reply_to, user_id, session_id)

    def _switch_to_session(self, chat_id: int, reply_to: int, user_id: int, session_id: str) -> None:
        meta = self.sessions.find_by_id(session_id)
        if not meta:
            self._send_message(chat_id, f"未找到 session: {session_id}", reply_to=reply_to)
            return
        self.state.set_active_session(user_id, meta.session_id, meta.cwd)
        self.state.set_heartbeat_context(user_id, meta.session_id, meta.cwd)
        self.state.set_pending_session_pick(user_id, False)
        self._send_message(
            chat_id,
            f"已切换到:\n{meta.title}\nsession: {meta.session_id}\ncwd: {meta.cwd}\n现在可直接发消息对话。",
            reply_to=reply_to,
        )

    def _try_handle_quick_session_pick(self, chat_id: int, reply_to: int, user_id: int, text: str) -> bool:
        if not self.state.is_pending_session_pick(user_id):
            return False
        raw = text.strip()
        if not raw.isdigit():
            return False
        idx = int(raw)
        recent_ids = self.state.get_last_session_ids(user_id)
        if idx <= 0 or idx > len(recent_ids):
            self._send_message(
                chat_id,
                "编号无效。请发送 /sessions 重新查看列表。",
                reply_to=reply_to,
            )
            return True
        self._switch_to_session(chat_id, reply_to, user_id, recent_ids[idx - 1])
        return True

    def _handle_history(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        tokens = [x for x in arg.split() if x]
        limit = 10
        session_id: Optional[str] = None

        if not tokens:
            session_id, _ = self.state.get_active(user_id)
            if not session_id:
                self._send_message(
                    chat_id,
                    "当前无 active session。先 /use 选择会话，或直接对话后再查看历史。",
                    reply_to=reply_to,
                )
                return
        else:
            session_id, err = self._resolve_session_selector(user_id, tokens[0])
            if err:
                self._send_message(chat_id, err, reply_to=reply_to)
                return
            if not session_id:
                self._send_message(chat_id, "无效的会话选择参数。", reply_to=reply_to)
                return
            if len(tokens) >= 2:
                try:
                    limit = int(tokens[1])
                except ValueError:
                    self._send_message(chat_id, "N 必须是数字，示例: /history 1 20", reply_to=reply_to)
                    return

        limit = max(1, min(50, limit))
        meta, messages = self.sessions.get_history(session_id, limit=limit)
        if not meta:
            self._send_message(chat_id, f"未找到 session: {session_id}", reply_to=reply_to)
            return
        if not messages:
            self._send_message(chat_id, "该会话暂无可展示历史消息。", reply_to=reply_to)
            return

        lines = [
            f"会话历史: {meta.title}",
            f"session: {meta.session_id}",
            f"显示最近 {len(messages)} 条消息:",
        ]
        for i, (role, message) in enumerate(messages, start=1):
            role_zh = "用户" if role == "user" else "助手"
            lines.append(f"{i}. [{role_zh}] {SessionStore.compact_message(message)}")
        self._send_message(chat_id, "\n".join(lines), reply_to=reply_to)

    def _resolve_session_selector(self, user_id: int, selector: str) -> Tuple[Optional[str], Optional[str]]:
        raw = selector.strip()
        if not raw:
            return None, "示例: /use 1 或 /use <session_id>"
        if raw.isdigit():
            idx = int(raw)
            recent_ids = self.state.get_last_session_ids(user_id)
            if idx <= 0 or idx > len(recent_ids):
                return None, "编号无效。先执行 /sessions，再用编号。"
            return recent_ids[idx - 1], None
        return raw, None

    def _format_memory_item_line(self, item: Dict[str, Any]) -> str:
        memory_id = str(item.get("id") or "--------")
        prefix = f"[{memory_id}]"
        if item.get("pinned"):
            prefix += "[置顶]"
        prefix += f"[{self._memory_category_label(item.get('category'))}]"
        text = SessionStore.compact_message(str(item.get("text") or ""), limit=120)
        return f"{prefix} {text}"

    def _handle_memory(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        raw = (arg or "").strip()
        if not raw or raw.lower() == "list":
            memories = self.memory_store.list_memories(user_id, limit=MEMORY_MAX_LIST_ITEMS)
            if not memories:
                self._send_message(chat_id, "现在还没有记住什么。你继续和我聊，我会慢慢记住该记的。", reply_to=reply_to)
                return
            lines = ["记忆库："]
            lines.extend(self._format_memory_item_line(item) for item in memories)
            lines.append("用法：/memory add <内容> | /memory forget <id> | /memory pin <id> | /memory unpin <id>")
            self._send_message(chat_id, "\n".join(lines), reply_to=reply_to)
            return

        action, _, remainder = raw.partition(" ")
        action = action.lower().strip()
        payload = remainder.strip()

        if action == "add":
            if not payload:
                self._send_message(chat_id, f"示例：/memory add {self.user_display_name}不喜欢板书式分点", reply_to=reply_to)
                return
            record = self.memory_store.add_memory(
                user_id,
                payload,
                category="general",
                source="manual",
                pinned=False,
            )
            if not record:
                self._send_message(chat_id, "这条记忆是空的，我没法记。", reply_to=reply_to)
                return
            self._send_message(chat_id, f"记住了：{self._format_memory_item_line(record)}", reply_to=reply_to)
            return

        if action in {"forget", "delete", "rm"}:
            if not payload:
                self._send_message(chat_id, "示例：/memory forget ab12cd34", reply_to=reply_to)
                return
            ok = self.memory_store.delete_memory(user_id, payload)
            if not ok:
                self._send_message(chat_id, f"没找到这条记忆：{payload}", reply_to=reply_to)
                return
            self._send_message(chat_id, f"这条记忆我已经删掉了：{payload}", reply_to=reply_to)
            return

        if action in {"pin", "unpin"}:
            if not payload:
                self._send_message(chat_id, f"示例：/memory {action} ab12cd34", reply_to=reply_to)
                return
            record = self.memory_store.set_pinned(user_id, payload, pinned=(action == "pin"))
            if not record:
                self._send_message(chat_id, f"没找到这条记忆：{payload}", reply_to=reply_to)
                return
            verb = "置顶了" if action == "pin" else "取消置顶了"
            self._send_message(chat_id, f"{verb}：{self._format_memory_item_line(record)}", reply_to=reply_to)
            return

        if action == "search":
            if not payload:
                self._send_message(chat_id, "示例：/memory search 电影", reply_to=reply_to)
                return
            memories = self.memory_store.search_memories(user_id, payload, limit=MEMORY_MAX_LIST_ITEMS)
            if not memories:
                self._send_message(chat_id, f"没搜到和“{payload}”相关的记忆。", reply_to=reply_to)
                return
            lines = [f"和“{payload}”相关的记忆："]
            lines.extend(self._format_memory_item_line(item) for item in memories)
            self._send_message(chat_id, "\n".join(lines), reply_to=reply_to)
            return

        self._send_message(
            chat_id,
            "用法：/memory | /memory add <内容> | /memory forget <id> | /memory pin <id> | /memory unpin <id> | /memory search <关键词>",
            reply_to=reply_to,
        )

    def _handle_voice(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        raw = (arg or "").strip()
        if not raw or raw.lower() == "status":
            self._send_message(chat_id, "\n".join(self._tts_status_lines(user_id)), reply_to=reply_to)
            return

        action, _, remainder = raw.partition(" ")
        action = action.lower().strip()
        payload = remainder.strip()

        if action == "key":
            if not payload:
                self._send_message(chat_id, "示例：/voice key sk-xxxxx", reply_to=reply_to)
                return
            self.state.update_voice_settings(user_id, api_key=payload)
            self._send_message(
                chat_id,
                f"语音 API key 记住了，现在是 {self._mask_secret(payload)}。",
                reply_to=reply_to,
            )
            return

        if action == "voice":
            if not payload:
                self._send_message(chat_id, "示例：/voice voice male-qn-qingse", reply_to=reply_to)
                return
            self.state.update_voice_settings(user_id, voice_id=payload)
            self._send_message(chat_id, f"语音音色已经换成 {payload}。", reply_to=reply_to)
            return

        if action in {"freq", "frequency"}:
            if not payload:
                self._send_message(chat_id, "示例：/voice freq medium", reply_to=reply_to)
                return
            if not _is_known_tts_frequency(payload):
                self._send_message(chat_id, "频率只支持 high / medium / low，也可以直接发 高 / 中 / 低。", reply_to=reply_to)
                return
            normalized = _normalize_tts_frequency(payload)
            self.state.update_voice_settings(user_id, frequency=normalized)
            label = TTS_FREQUENCY_LABELS.get(normalized, "中")
            self._send_message(chat_id, f"语音频率已经调成{label}档。", reply_to=reply_to)
            return

        if action == "clear":
            self.state.update_voice_settings(user_id, clear=True)
            self._send_message(chat_id, "语音配置已经清空了。", reply_to=reply_to)
            return

        self._send_message(
            chat_id,
            "用法：/voice | /voice key <API_KEY> | /voice voice <voice_id> | /voice freq <high|medium|low> | /voice clear",
            reply_to=reply_to,
        )

    def _handle_status(self, chat_id: int, reply_to: int, user_id: int) -> None:
        session_id, cwd = self.state.get_active(user_id)
        running_count = self.running_prompts.count(user_id)
        if not session_id:
            message = "当前没有绑定会话。可先 /sessions + /use，或 /new 后直接发消息。"
            if running_count > 0:
                message += f"\n后台仍有 {running_count} 个任务运行，可继续 /use 切线程。"
            self._send_message(
                chat_id,
                message,
                reply_to=reply_to,
            )
            return
        title = f"session {session_id[:8]}"
        meta = self.sessions.find_by_id(session_id)
        if meta:
            title = meta.title
        lines = [
            "当前会话:",
            title,
            f"session: {session_id}",
            f"cwd: {cwd or str(self.default_cwd)}",
            "支持与本地 Codex 客户端交替续聊。",
        ]
        if running_count > 0:
            lines.append(f"后台运行中: {running_count} 个任务（可继续 /use 切线程）")
        self._send_message(
            chat_id,
            "\n".join(lines),
            reply_to=reply_to,
        )

    def _handle_heartbeat(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        tokens = [part for part in arg.split() if part]
        action = tokens[0].lower() if tokens else "status"

        if action == "status":
            self._send_message(chat_id, "\n".join(self._heartbeat_status_lines(user_id)), reply_to=reply_to)
            return

        if action == "off":
            self.state.configure_heartbeat(user_id, enabled=False)
            self._send_message(chat_id, "心跳模式关掉了。等你来找我，我再回你。", reply_to=reply_to)
            return

        if action == "now":
            if self._trigger_heartbeat(chat_id, user_id, force=True):
                self._send_message(chat_id, "这就主动来找你一次。", reply_to=reply_to)
            else:
                self._send_message(chat_id, "你现在这条会话还在忙，我等它空下来再来戳你。", reply_to=reply_to)
            return

        interval_minutes: Optional[int] = None
        if action == "on":
            if len(tokens) >= 2:
                try:
                    interval_minutes = int(tokens[1])
                except ValueError:
                    self._send_message(chat_id, "示例：/heartbeat on 30", reply_to=reply_to)
                    return
        else:
            try:
                interval_minutes = int(action)
                action = "on"
            except ValueError:
                self._send_message(chat_id, "用法：/heartbeat on 30 | /heartbeat off | /heartbeat now", reply_to=reply_to)
                return

        if action == "on":
            if interval_minutes is None:
                interval_minutes = max(1, self.heartbeat_default_interval_sec // 60)
            if interval_minutes <= 0:
                self._send_message(chat_id, "间隔得大于 0 分钟。", reply_to=reply_to)
                return
            interval_sec = max(HEARTBEAT_MIN_INTERVAL_SEC, interval_minutes * 60)
            self.state.configure_heartbeat(user_id, enabled=True, interval_sec=interval_sec)
            lines = [
                f"心跳模式开了。每 {max(1, interval_sec // 60)} 分钟看看你一次。",
                "你没绑定活动会话时，我会发轻一点的提醒。",
                "你绑着活动会话时，我会尽量按上下文来找你。",
            ]
            self._send_message(chat_id, "\n".join(lines), reply_to=reply_to)
            return

        self._send_message(chat_id, "\n".join(self._heartbeat_status_lines(user_id)), reply_to=reply_to)

    def _handle_ask(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        arg: str,
        message_ts: Optional[int] = None,
    ) -> None:
        prompt = arg.strip()
        if not prompt:
            self._send_message(chat_id, "示例: /ask 帮我总结当前仓库结构", reply_to=reply_to)
            return
        self._run_prompt(
            chat_id,
            reply_to,
            user_id,
            self._decorate_text_prompt_with_context(prompt, message_ts),
            memory_source_text=prompt,
        )

    def _handle_new(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        cwd_raw = arg.strip()
        _, current_cwd = self.state.get_active(user_id)
        target_cwd = Path(current_cwd).expanduser() if current_cwd else self.default_cwd
        if cwd_raw:
            candidate = Path(cwd_raw).expanduser()
            if not candidate.exists() or not candidate.is_dir():
                self._send_message(chat_id, f"cwd 不存在或不是目录: {candidate}", reply_to=reply_to)
                return
            target_cwd = candidate
        self.state.clear_active_session(user_id, str(target_cwd))
        self.state.set_pending_session_pick(user_id, False)
        self._send_message(
            chat_id,
            f"已进入新会话模式，cwd: {target_cwd}\n下一条普通消息会创建一个新 session。",
            reply_to=reply_to,
        )

    def _handle_chat_message(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        text: str,
        message_ts: Optional[int] = None,
    ) -> None:
        self._run_prompt(
            chat_id,
            reply_to,
            user_id,
            self._decorate_text_prompt_with_context(text, message_ts),
            memory_source_text=text,
        )

    def _handle_audio_message(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        media: Dict[str, Any],
        caption: str,
        kind: str,
        message_ts: Optional[int] = None,
    ) -> None:
        if self.audio_transcriber is None:
            self._send_message(
                chat_id,
                "当前未配置语音转写。设置 OPENAI_API_KEY 后，可直接发送 Telegram 语音或音频消息。",
                reply_to=reply_to,
            )
            return

        file_id = str(media.get("file_id") or "").strip()
        if not file_id:
            self._send_message(chat_id, "无法读取这条语音消息的文件 ID。", reply_to=reply_to)
            return

        active_id, active_cwd = self.state.get_active(user_id)
        cwd = Path(active_cwd).expanduser() if active_cwd else self.default_cwd
        if not cwd.exists():
            cwd = self.default_cwd
        if not self.running_prompts.try_start(user_id, active_id):
            busy_session = active_id[:8] if active_id else "当前线程"
            self._send_message(
                chat_id,
                f"会话 {busy_session} 已有任务运行中。可先 /use 切到其他线程，或等待当前回复完成。",
                reply_to=reply_to,
            )
            return

        session_label = self._session_label(active_id, cwd)
        log(
            f"queue audio prompt: user_id={user_id} kind={kind} cwd={cwd} "
            f"session={active_id} caption_len={len(caption)}"
        )
        if not self.stream_enabled:
            self._send_message(
                chat_id,
                "已开始处理。\n可继续发送 /use、/sessions、/status。",
                reply_to=reply_to,
            )
        worker = threading.Thread(
            target=self._run_audio_prompt_worker,
            args=(chat_id, reply_to, user_id, active_id, cwd, session_label, media, caption, kind, message_ts),
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            self.running_prompts.finish(user_id, active_id)
            raise

    def _handle_attachment_message(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        media: Dict[str, Any],
        caption: str,
        kind: str,
        message_ts: Optional[int] = None,
    ) -> None:
        file_id = str(media.get("file_id") or "").strip()
        if not file_id:
            self._send_message(chat_id, "无法读取这条附件消息的文件 ID。", reply_to=reply_to)
            return

        active_id, active_cwd = self.state.get_active(user_id)
        cwd = Path(active_cwd).expanduser() if active_cwd else self.default_cwd
        if not cwd.exists():
            cwd = self.default_cwd
        if not self.running_prompts.try_start(user_id, active_id):
            busy_session = active_id[:8] if active_id else "当前线程"
            self._send_message(
                chat_id,
                f"会话 {busy_session} 已有任务运行中。可先 /use 切到其他线程，或等待当前回复完成。",
                reply_to=reply_to,
            )
            return

        session_label = self._session_label(active_id, cwd)
        log(
            f"queue attachment prompt: user_id={user_id} kind={kind} cwd={cwd} "
            f"session={active_id} caption_len={len(caption)}"
        )
        if not self.stream_enabled:
            self._send_message(
                chat_id,
                "已开始处理附件。\n可继续发送 /use、/sessions、/status。",
                reply_to=reply_to,
            )

        worker = threading.Thread(
            target=self._run_attachment_prompt_worker,
            args=(chat_id, reply_to, user_id, active_id, cwd, session_label, media, caption, kind, message_ts),
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            self.running_prompts.finish(user_id, active_id)
            raise

    def _session_label(self, session_id: Optional[str], cwd: Path) -> str:
        resolved_cwd = cwd
        if session_id:
            meta = self.sessions.find_by_id(session_id)
            title = meta.title if meta else f"session {session_id[:8]}"
            if meta and meta.cwd:
                resolved_cwd = Path(meta.cwd)
        else:
            title = "新会话"
        cwd_name = resolved_cwd.name or str(resolved_cwd)
        if session_id:
            return f"{title} | {session_id[:8]} | {cwd_name}"
        return f"{title} | {cwd_name}"

    def _initial_prompt_status(self, session_label: str, active_id: Optional[str], elapsed: Optional[int] = None) -> str:
        body = "思考中..."
        if elapsed is not None:
            body = f"{body}\n\n已等待 {elapsed}s"
        return self._format_prompt_response(session_label, body)

    @staticmethod
    def _format_prompt_response(session_label: str, text: str) -> str:
        return (text or "Codex 没有返回可展示内容。").strip() or "Codex 没有返回可展示内容。"

    @staticmethod
    def _stream_preview_text(text: str) -> str:
        raw = text.strip() or "..."
        suffix = "\n\n[生成中...]"
        max_size = min(3800, MAX_TELEGRAM_TEXT)
        if len(raw) + len(suffix) <= max_size:
            return raw + suffix
        keep = max_size - len(suffix) - 1
        if keep <= 0:
            return raw[:max_size]
        return raw[:keep] + "…" + suffix

    def _finalize_stream_reply(
        self,
        chat_id: int,
        reply_to: int,
        stream_message_id: Optional[int],
        text: str,
        progressive_replay: bool = False,
        user_id: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        parts = self._conversation_parts(text or "Codex 没有返回可展示内容。")
        if not parts:
            parts = ["Codex 没有返回可展示内容。"]
        log(
            "conversation finalize: "
            f"chat_id={chat_id} parts={len(parts)} lens={[len(part) for part in parts[:8]]} "
            f"stream_message_id={stream_message_id}"
        )

        first_sent = False
        if stream_message_id is not None:
            try:
                self.api.edit_message_text(
                    chat_id,
                    stream_message_id,
                    parts[0],
                    reply_markup=reply_markup if len(parts) == 1 else None,
                )
                first_sent = True
            except Exception as e:
                log(f"stream final edit failed: {e}")

        if not first_sent:
            self._send_message(
                chat_id,
                parts[0],
                reply_to=reply_to,
                user_id=user_id,
                reply_markup=reply_markup if len(parts) == 1 else None,
            )
            stream_message_id = None

        if progressive_replay and stream_message_id is not None and len(parts) == 1 and len(parts[0]) > 240:
            full = parts[0]
            step = 120
            interval_sec = 0.12
            for end in range(step, len(full), step):
                partial = full[:end].rstrip()
                if not partial:
                    continue
                preview = f"{partial}\n\n[生成中...]"
                try:
                    self.api.edit_message_text(chat_id, stream_message_id, preview)
                except Exception:
                    stream_message_id = None
                    break
                time.sleep(interval_sec)
            if stream_message_id is not None:
                try:
                    self.api.edit_message_text(
                        chat_id,
                        stream_message_id,
                        full,
                        reply_markup=reply_markup if len(parts) == 1 else None,
                    )
                except Exception:
                    stream_message_id = None

        for idx, part in enumerate(parts[1:], start=1):
            self._send_message(
                chat_id,
                part,
                user_id=user_id,
                reply_markup=reply_markup if idx == len(parts) - 1 else None,
            )
            time.sleep(CONVERSATION_PART_DELAY_SEC)

    def _run_prompt(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        prompt: str,
        image_paths: Optional[List[Path]] = None,
        memory_source_text: Optional[str] = None,
    ) -> None:
        active_id, active_cwd = self.state.get_active(user_id)
        cwd = Path(active_cwd).expanduser() if active_cwd else self.default_cwd
        if not cwd.exists():
            cwd = self.default_cwd
        if not self.running_prompts.try_start(user_id, active_id):
            busy_session = active_id[:8] if active_id else "当前线程"
            self._send_message(
                chat_id,
                f"会话 {busy_session} 已有任务运行中。可先 /use 切到其他线程，或等待当前回复完成。",
                reply_to=reply_to,
            )
            return

        session_label = self._session_label(active_id, cwd)
        mode = "继续当前会话" if active_id else "新建会话"
        log(f"queue prompt: user_id={user_id} mode={mode} cwd={cwd} session={active_id}")
        if not self.stream_enabled:
            self._send_message(
                chat_id,
                "已开始处理。\n可继续发送 /use、/sessions、/status。",
                reply_to=reply_to,
            )

        worker = threading.Thread(
            target=self._run_prompt_worker,
            args=(chat_id, reply_to, user_id, prompt, active_id, cwd, session_label, image_paths, memory_source_text),
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            self.running_prompts.finish(user_id, active_id)
            raise

    def _run_prompt_worker(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        prompt: str,
        active_id: Optional[str],
        cwd: Path,
        session_label: str,
        image_paths: Optional[List[Path]] = None,
        memory_source_text: Optional[str] = None,
    ) -> None:
        prompt = self._decorate_new_thread_prompt(prompt, active_id)
        prompt = self._decorate_prompt_with_memory_context(user_id, prompt, memory_source_text, active_id)
        if not active_id and self.new_thread_persona_enabled:
            log(f"new thread persona injected: user_id={user_id} chat_id={chat_id}")
        stream_message_id: Optional[int] = None
        stream_lock = threading.Lock()
        thinking_stop = threading.Event()
        first_output = threading.Event()
        thinking_thread: Optional[threading.Thread] = None
        stream_state: Dict[str, Any] = {
            "last_preview": "",
            "last_emit_at_ms": 0,
            "content_updates": 0,
        }
        run_started_at = time.time()
        first_output_at: List[float] = []

        def edit_stream_message(text: str) -> bool:
            nonlocal stream_message_id
            if stream_message_id is None:
                return False
            with stream_lock:
                current_id = stream_message_id
                if current_id is None:
                    return False
                try:
                    self.api.edit_message_text(chat_id, current_id, text)
                    return True
                except Exception as e:
                    log(f"stream edit failed: {e}")
                    stream_message_id = None
                    return False

        if self.stream_enabled:
            try:
                sent = self._send_message_with_result(
                    chat_id,
                    self._initial_prompt_status(session_label, active_id),
                    reply_to=reply_to,
                )
                msg_id = sent.get("message_id")
                if isinstance(msg_id, int):
                    stream_message_id = msg_id
            except Exception as e:
                log(f"stream placeholder send failed: {e}")

        def thinking_loop() -> None:
            phases = ["思考中", "思考中.", "思考中..", "思考中..."]
            start_ts = time.time()
            i = 0
            while not thinking_stop.wait(self.thinking_status_interval_ms / 1000.0):
                if first_output.is_set():
                    return
                elapsed = int(time.time() - start_ts)
                status_text = self._format_prompt_response(
                    session_label,
                    f"{phases[i % len(phases)]}\n\n已等待 {elapsed}s",
                )
                i += 1
                if not edit_stream_message(status_text):
                    return

        if stream_message_id is not None:
            thinking_thread = threading.Thread(target=thinking_loop, daemon=True)
            thinking_thread.start()

        def on_update(live_text: str) -> None:
            first_output.set()
            if not first_output_at:
                first_output_at.append(time.time())
            if stream_message_id is None:
                return
            preview = self._format_prompt_response(
                session_label,
                self._stream_preview_text(live_text),
            )
            now_ms = int(time.time() * 1000)
            last_preview = str(stream_state.get("last_preview") or "")
            last_emit_at_ms = int(stream_state.get("last_emit_at_ms") or 0)
            if preview == last_preview:
                return
            # Throttle edit frequency to avoid Telegram 429.
            delta_chars = abs(len(preview) - len(last_preview))
            if now_ms - last_emit_at_ms < self.stream_edit_interval_ms and delta_chars < self.stream_min_delta_chars:
                return
            ok = edit_stream_message(preview)
            if not ok:
                return
            stream_state["last_preview"] = preview
            stream_state["last_emit_at_ms"] = now_ms
            stream_state["content_updates"] = int(stream_state.get("content_updates") or 0) + 1

        typing = TypingStatus(self.api, chat_id)
        typing.start()
        try:
            run_kwargs: Dict[str, Any] = {
                "prompt": prompt,
                "cwd": cwd,
                "session_id": active_id,
                "on_update": on_update if stream_message_id is not None else None,
            }
            if image_paths:
                run_kwargs["image_paths"] = image_paths
            thread_id, answer, stderr_text, return_code = self.codex.run_prompt(
                **run_kwargs,
            )
        except Exception as e:
            thinking_stop.set()
            if thinking_thread is not None:
                thinking_thread.join(timeout=0.3)
            err_msg = self._format_prompt_response(
                session_label,
                f"调用 Codex 时出现异常: {e}",
            )
            if stream_message_id is not None:
                self._finalize_stream_reply(chat_id, reply_to, stream_message_id, err_msg, progressive_replay=False)
            else:
                self._send_conversation_message(chat_id, err_msg, reply_to=reply_to, user_id=user_id)
            return
        finally:
            thinking_stop.set()
            if thinking_thread is not None:
                thinking_thread.join(timeout=0.3)
            typing.stop()
            self.running_prompts.finish(user_id, active_id)

        elapsed_sec = round(time.time() - run_started_at, 2)
        first_output_sec = round(first_output_at[0] - run_started_at, 2) if first_output_at else None
        log(
            "prompt finished: "
            f"user_id={user_id} session={active_id} thread={thread_id} exit={return_code} "
            f"elapsed_sec={elapsed_sec} first_output_sec={first_output_sec}"
        )

        final_session_id = thread_id or active_id
        final_session_label = self._session_label(final_session_id, cwd)
        if final_session_id:
            # Tag bot-created/continued sessions so Codex Desktop can surface them like local chats.
            self.sessions.mark_as_desktop_session(final_session_id)
            self.state.set_heartbeat_context(user_id, final_session_id, str(cwd))
        session_updated = False
        if thread_id:
            session_updated = self.state.update_active_session_if_unchanged(
                user_id,
                active_id,
                thread_id,
                str(cwd),
            )

        if return_code != 0:
            msg = f"Codex 执行失败 (exit={return_code})\n{answer}"
            if stderr_text:
                msg += f"\n\nstderr:\n{stderr_text[-1200:]}"
            msg = self._format_prompt_response(final_session_label, msg)
            if stream_message_id is not None:
                self._finalize_stream_reply(chat_id, reply_to, stream_message_id, msg, progressive_replay=False)
            else:
                self._send_conversation_message(chat_id, msg, reply_to=reply_to, user_id=user_id)
            return

        if thread_id and not session_updated:
            current_active_id, _ = self.state.get_active(user_id)
            if current_active_id != thread_id:
                note = "当前活动线程未变；这是后台线程的回复。"
                if not active_id:
                    note = "新线程已创建，但你已经切到别的线程，当前活动线程未变。"
                answer = f"{note}\n\n{answer}"

        self._schedule_memory_writeback(user_id, cwd, memory_source_text)
        answer = self._format_prompt_response(final_session_label, answer)
        delivery_segments = self._build_reply_delivery_segments(answer, user_id)

        if stream_message_id is not None:
            replay = int(stream_state.get("content_updates") or 0) == 0
            self._send_delivery_segments(
                chat_id,
                reply_to,
                user_id,
                delivery_segments,
                stream_message_id=stream_message_id,
                progressive_replay=replay,
            )
            return

        self._send_delivery_segments(
            chat_id,
            reply_to,
            user_id,
            delivery_segments,
        )

    def _run_audio_prompt_worker(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        active_id: Optional[str],
        cwd: Path,
        session_label: str,
        media: Dict[str, Any],
        caption: str,
        kind: str,
        message_ts: Optional[int] = None,
    ) -> None:
        if self.audio_transcriber is None:
            self.running_prompts.finish(user_id, active_id)
            self._send_message(
                chat_id,
                "当前未配置语音转写。设置 OPENAI_API_KEY 后，可直接发送 Telegram 语音或音频消息。",
                reply_to=reply_to,
            )
            return

        file_id = str(media.get("file_id") or "").strip()
        file_name = media.get("file_name")
        mime_type = media.get("mime_type")
        file_size_raw = media.get("file_size")
        file_size = file_size_raw if isinstance(file_size_raw, int) else None

        typing = TypingStatus(self.api, chat_id)
        typing.start()
        try:
            transcript = self.audio_transcriber.transcribe_telegram_audio(
                self.api,
                file_id=file_id,
                file_name=str(file_name).strip() if file_name else None,
                mime_type=str(mime_type).strip() if mime_type else None,
                file_size=file_size,
            )
        except Exception as e:
            log(f"audio transcription failed: user_id={user_id} kind={kind} error={e}")
            self._send_message(chat_id, f"语音转写失败: {e}", reply_to=reply_to)
            self.running_prompts.finish(user_id, active_id)
            return
        finally:
            typing.stop()

        transcript = transcript.strip()
        if not transcript:
            self._send_message(chat_id, "语音转写结果为空，未继续发送给 Codex。", reply_to=reply_to)
            self.running_prompts.finish(user_id, active_id)
            return

        prompt = self._decorate_audio_prompt_with_context(transcript, caption, message_ts)
        log(
            f"audio transcription finished: user_id={user_id} kind={kind} "
            f"session={active_id} transcript_len={len(transcript)}"
        )
        self._run_prompt_worker(
            chat_id=chat_id,
            reply_to=reply_to,
            user_id=user_id,
            prompt=prompt,
            active_id=active_id,
            cwd=cwd,
            session_label=session_label,
            memory_source_text="\n".join(part for part in [caption.strip(), transcript] if part.strip()),
        )

    def _run_attachment_prompt_worker(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        active_id: Optional[str],
        cwd: Path,
        session_label: str,
        media: Dict[str, Any],
        caption: str,
        kind: str,
        message_ts: Optional[int] = None,
    ) -> None:
        typing = TypingStatus(self.api, chat_id)
        typing.start()
        try:
            attachment = self._write_telegram_attachment(cwd, media, kind=kind)
        except Exception as e:
            log(f"attachment fetch failed: user_id={user_id} kind={kind} error={e}")
            self._send_message(chat_id, f"附件读取失败: {e}", reply_to=reply_to)
            self.running_prompts.finish(user_id, active_id)
            return
        finally:
            typing.stop()

        prompt = self._decorate_attachment_prompt_with_context(attachment, caption, message_ts)
        image_paths = [attachment.local_path] if attachment.is_image else None
        log(
            f"attachment ready: user_id={user_id} kind={kind} session={active_id} "
            f"path={attachment.local_path} size={attachment.size_bytes} image={attachment.is_image}"
        )
        self._run_prompt_worker(
            chat_id=chat_id,
            reply_to=reply_to,
            user_id=user_id,
            prompt=prompt,
            active_id=active_id,
            cwd=cwd,
            session_label=session_label,
            image_paths=image_paths,
            memory_source_text=caption,
        )

    def _run_tts_callback_worker(
        self,
        chat_id: int,
        reply_to: Optional[int],
        user_id: int,
        token: str,
    ) -> None:
        request = self.state.get_tts_request(user_id, token)
        if not request:
            self._send_message(chat_id, "这条语音入口已经失效了。", reply_to=reply_to)
            return

        text = str(request.get("text") or "").strip()
        if not text:
            self._send_message(chat_id, "这条语音内容是空的，我没法开口。", reply_to=reply_to)
            return

        self._deliver_tts_voice(
            chat_id,
            reply_to=reply_to,
            user_id=user_id,
            text=text,
            request=request,
            notify_errors=True,
        )

    def _run_tts_reply_worker(
        self,
        chat_id: int,
        reply_to: Optional[int],
        user_id: int,
        text: str,
    ) -> None:
        self._deliver_tts_voice(
            chat_id,
            reply_to=reply_to,
            user_id=user_id,
            text=text,
            request=None,
            notify_errors=False,
        )

    def _deliver_tts_voice(
        self,
        chat_id: int,
        *,
        reply_to: Optional[int],
        user_id: int,
        text: str,
        request: Optional[Dict[str, Any]],
        notify_errors: bool,
    ) -> bool:
        cleaned = str(text or "").strip()
        if not cleaned:
            if notify_errors:
                self._send_message(chat_id, "这条语音内容是空的，我没法开口。", reply_to=reply_to)
            return False

        try:
            synth = self._build_user_tts_synthesizer(user_id, request)
        except Exception as e:
            log(f"tts synthesizer unavailable: user_id={user_id} error={e}")
            if notify_errors:
                self._send_message(chat_id, f"语音配置不可用：{e}", reply_to=reply_to)
            return False

        if synth is None:
            if notify_errors:
                self._send_message(chat_id, "你还没配好语音 key 或 voice_id。", reply_to=reply_to)
            return False

        try:
            self.api.send_chat_action(chat_id, "record_voice")
        except Exception:
            pass

        try:
            voice = synth.synthesize_voice_note(cleaned)
            try:
                self.api.send_chat_action(chat_id, "upload_voice")
            except Exception:
                pass
            self._send_voice(chat_id, voice, reply_to=reply_to, user_id=user_id)
            log(
                "tts voice sent: "
                f"chat_id={chat_id} user_id={user_id} bytes={len(voice.audio_bytes)} backend={self.tts_backend}"
            )
            return True
        except Exception as e:
            log(f"tts voice generation failed: chat_id={chat_id} user_id={user_id} error={e}")
            if notify_errors:
                self._send_message(chat_id, f"语音生成失败：{e}", reply_to=reply_to)
            return False

def build_service() -> TgCodexService:
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("missing TELEGRAM_BOT_TOKEN")

    allowed_user_ids = parse_allowed_user_ids(env("ALLOWED_TELEGRAM_USER_IDS"))
    require_allowlist = parse_bool_env(env("TG_REQUIRE_ALLOWLIST"), True)
    session_root = Path(env("CODEX_SESSION_ROOT", "~/.codex/sessions")).expanduser()
    state_path = Path(env("STATE_PATH", "./bot_state.json"))
    memory_path = Path(
        env(
            "TG_MEMORY_PATH",
            env("MEMORY_PATH", str(state_path.with_name("bot_memory.json"))),
        )
    ).expanduser()
    codex_bin = resolve_codex_bin(env("CODEX_BIN"))
    codex_sandbox_mode = env("CODEX_SANDBOX_MODE")
    codex_approval_policy = env("CODEX_APPROVAL_POLICY")
    codex_dangerous_bypass_level = parse_dangerous_bypass_level(env("CODEX_DANGEROUS_BYPASS", "0"))
    codex_idle_timeout_sec = parse_non_negative_int(
        env("CODEX_IDLE_TIMEOUT_SEC", env("CODEX_EXEC_TIMEOUT_SEC", "3600")),
        3600,
    )
    openai_api_key = env("OPENAI_API_KEY")
    openai_api_base = env("OPENAI_BASE_URL", "https://api.openai.com/v1")
    tg_voice_enabled_raw = env("TG_VOICE_TRANSCRIBE_ENABLED")
    tg_voice_enabled = True if tg_voice_enabled_raw is None else tg_voice_enabled_raw == "1"
    tg_voice_backend = env("TG_VOICE_TRANSCRIBE_BACKEND", "local-whisper")
    tg_voice_model = env("TG_VOICE_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
    tg_voice_timeout_sec = parse_non_negative_int(env("TG_VOICE_TRANSCRIBE_TIMEOUT_SEC", "180"), 180)
    tg_voice_max_bytes = parse_non_negative_int(env("TG_VOICE_MAX_BYTES", "26214400"), 26214400)
    tg_voice_local_model = env("TG_VOICE_LOCAL_MODEL", "base")
    tg_voice_local_device = env("TG_VOICE_LOCAL_DEVICE", "cpu")
    tg_voice_local_language = env("TG_VOICE_LOCAL_LANGUAGE")
    tg_voice_ffmpeg_bin = env("TG_VOICE_FFMPEG_BIN")
    tg_tts_enabled = parse_bool_env(env("TG_TTS_ENABLED"), False)
    tg_tts_backend = env("TG_TTS_BACKEND", "minimax")
    tg_tts_mode = env("TG_TTS_MODE", "auto")
    tg_tts_api_base_default = (
        DEFAULT_TTS_API_BASE if (tg_tts_backend or "").strip().lower() == "local-gpt-sovits" else DEFAULT_MINIMAX_API_BASE
    )
    tg_tts_api_base = env("TG_TTS_API_BASE", tg_tts_api_base_default)
    tg_tts_model_default = env("TG_TTS_MODEL_DEFAULT", DEFAULT_MINIMAX_MODEL)
    tg_tts_gsv_root = env("TG_TTS_GSV_ROOT")
    tg_tts_ref_audio_path = env("TG_TTS_REF_AUDIO_PATH")
    tg_tts_prompt_text = env("TG_TTS_PROMPT_TEXT")
    tg_tts_prompt_lang = env("TG_TTS_PROMPT_LANG", "zh")
    tg_tts_text_lang = env("TG_TTS_TEXT_LANG", "zh")
    tg_tts_gpt_weights = env("TG_TTS_GPT_WEIGHTS")
    tg_tts_sovits_weights = env("TG_TTS_SOVITS_WEIGHTS")
    tg_tts_python_bin = env("TG_TTS_PYTHON_BIN")
    tg_tts_ffmpeg_bin = env("TG_TTS_FFMPEG_BIN")
    tg_tts_tts_config = env("TG_TTS_TTS_CONFIG")
    tg_tts_startup_timeout_sec = parse_non_negative_int(env("TG_TTS_STARTUP_TIMEOUT_SEC", "240"), 240)
    tg_tts_request_timeout_sec = parse_non_negative_int(env("TG_TTS_REQUEST_TIMEOUT_SEC", "240"), 240)
    tg_tts_speed_factor_raw = env("TG_TTS_SPEED_FACTOR", "1.0")
    tg_tts_max_chars = parse_non_negative_int(env("TG_TTS_MAX_CHARS", str(DEFAULT_TTS_MAX_CHARS)), DEFAULT_TTS_MAX_CHARS)
    tg_tts_cache_dir = Path(env("TG_TTS_CACHE_DIR", str(SCRIPT_DIR / ".runtime" / "tts-cache"))).expanduser()
    try:
        tg_tts_speed_factor = float(tg_tts_speed_factor_raw or "1.0")
    except ValueError:
        tg_tts_speed_factor = 1.0
    default_cwd = Path(env("DEFAULT_CWD", os.getcwd())).expanduser()
    ca_bundle = env("TELEGRAM_CA_BUNDLE")
    insecure_skip_verify = env("TELEGRAM_INSECURE_SKIP_VERIFY", "0") == "1"
    tg_stream_enabled = env("TG_STREAM_ENABLED", "1") == "1"
    tg_stream_edit_interval_ms = parse_non_negative_int(env("TG_STREAM_EDIT_INTERVAL_MS", "300"), 300)
    tg_stream_min_delta_chars = parse_non_negative_int(env("TG_STREAM_MIN_DELTA_CHARS", "8"), 8)
    tg_thinking_status_interval_ms = parse_non_negative_int(env("TG_THINKING_STATUS_INTERVAL_MS", "700"), 700)
    tg_reply_to_messages = parse_bool_env(env("TG_REPLY_TO_MESSAGES"), False)
    tg_attach_time_context = parse_bool_env(env("TG_ATTACH_TIME_CONTEXT"), True)
    tg_user_display_name = env("TG_USER_DISPLAY_NAME", "对方")
    tg_new_thread_persona_enabled = parse_bool_env(env("TG_NEW_THREAD_PERSONA_ENABLED"), True)
    tg_new_thread_persona_prompt = _load_text_override(
        NEW_THREAD_PERSONA_PROMPT,
        inline_env_name="TG_NEW_THREAD_PERSONA_PROMPT",
        path_env_name="TG_NEW_THREAD_PERSONA_PROMPT_PATH",
    )
    tg_heartbeat_session_prompt = _load_text_override(
        HEARTBEAT_SESSION_PROMPT,
        inline_env_name="TG_HEARTBEAT_SESSION_PROMPT",
        path_env_name="TG_HEARTBEAT_SESSION_PROMPT_PATH",
    )
    tg_heartbeat_banned_patterns = _load_list_override(
        HEARTBEAT_BANNED_PATTERNS,
        inline_env_name="TG_HEARTBEAT_BANNED_PATTERNS",
        path_env_name="TG_HEARTBEAT_BANNED_PATTERNS_PATH",
    )
    tg_heartbeat_template_messages = _load_list_override(
        HEARTBEAT_TEMPLATE_MESSAGES,
        inline_env_name="TG_HEARTBEAT_TEMPLATE_MESSAGES",
        path_env_name="TG_HEARTBEAT_TEMPLATE_MESSAGES_PATH",
    )
    tg_heartbeat_followup_template_messages = _load_list_override(
        HEARTBEAT_FOLLOWUP_TEMPLATE_MESSAGES,
        inline_env_name="TG_HEARTBEAT_FOLLOWUP_TEMPLATE_MESSAGES",
        path_env_name="TG_HEARTBEAT_FOLLOWUP_TEMPLATE_MESSAGES_PATH",
    )
    tg_memory_context_prompt = _load_text_override(
        MEMORY_CONTEXT_PROMPT,
        inline_env_name="TG_MEMORY_CONTEXT_PROMPT",
        path_env_name="TG_MEMORY_CONTEXT_PROMPT_PATH",
    )
    tg_memory_writeback_prompt = _load_text_override(
        MEMORY_WRITEBACK_PROMPT,
        inline_env_name="TG_MEMORY_WRITEBACK_PROMPT",
        path_env_name="TG_MEMORY_WRITEBACK_PROMPT_PATH",
    )
    memory_auto_enabled = parse_bool_env(env("TG_MEMORY_AUTO_ENABLED"), True)

    if require_allowlist and not allowed_user_ids:
        raise RuntimeError(
            "ALLOWED_TELEGRAM_USER_IDS is required by default for safety. "
            "Set your Telegram numeric user ID, or set TG_REQUIRE_ALLOWLIST=0 to override."
        )
    if insecure_skip_verify:
        log("warn: TELEGRAM_INSECURE_SKIP_VERIFY=1 disables TLS certificate verification")
    if codex_dangerous_bypass_level > 0:
        log(f"warn: CODEX_DANGEROUS_BYPASS={codex_dangerous_bypass_level} expands local machine risk")
    if default_cwd == Path.home() or str(default_cwd) == "/":
        log(f"warn: DEFAULT_CWD points to a broad directory: {default_cwd}")

    api = TelegramAPI(
        token=token,
        ca_bundle=ca_bundle,
        insecure_skip_verify=insecure_skip_verify,
    )
    sessions = SessionStore(session_root)
    state = BotState(state_path)
    memory_store = MemoryStore(memory_path)
    codex = CodexRunner(
        codex_bin=codex_bin,
        sandbox_mode=codex_sandbox_mode,
        approval_policy=codex_approval_policy,
        dangerous_bypass_level=codex_dangerous_bypass_level,
        idle_timeout_sec=codex_idle_timeout_sec,
    )
    audio_transcriber: Optional[AudioTranscriber] = None
    voice_backend_label = "disabled"
    if tg_voice_enabled:
        backend = (tg_voice_backend or "auto").strip().lower()
        if backend == "local-whisper":
            try:
                local_transcriber = LocalWhisperAudioTranscriber(
                    model_name=tg_voice_local_model,
                    ffmpeg_bin=tg_voice_ffmpeg_bin,
                    device=tg_voice_local_device,
                    language=tg_voice_local_language,
                    max_bytes=tg_voice_max_bytes,
                )
                local_transcriber.validate_environment()
                audio_transcriber = local_transcriber
                voice_backend_label = f"local-whisper:{tg_voice_local_model}"
            except Exception as e:
                voice_backend_label = f"local-whisper-unavailable:{e}"
        elif backend == "openai":
            if openai_api_key:
                audio_transcriber = OpenAIAudioTranscriber(
                    api_key=openai_api_key,
                    model=tg_voice_model,
                    api_base=openai_api_base,
                    timeout_sec=tg_voice_timeout_sec,
                    max_bytes=tg_voice_max_bytes,
                )
                voice_backend_label = f"openai:{tg_voice_model}"
            else:
                voice_backend_label = "openai-missing-key"
        else:
            try:
                local_transcriber = LocalWhisperAudioTranscriber(
                    model_name=tg_voice_local_model,
                    ffmpeg_bin=tg_voice_ffmpeg_bin,
                    device=tg_voice_local_device,
                    language=tg_voice_local_language,
                    max_bytes=tg_voice_max_bytes,
                )
                local_transcriber.validate_environment()
                audio_transcriber = local_transcriber
                voice_backend_label = f"local-whisper:{tg_voice_local_model}"
            except Exception:
                if openai_api_key:
                    audio_transcriber = OpenAIAudioTranscriber(
                        api_key=openai_api_key,
                        model=tg_voice_model,
                        api_base=openai_api_base,
                        timeout_sec=tg_voice_timeout_sec,
                        max_bytes=tg_voice_max_bytes,
                    )
                    voice_backend_label = f"openai:{tg_voice_model}"
                else:
                    voice_backend_label = "auto-unavailable"
    tts_synthesizer: Optional[LocalGptSovitsTtsSynthesizer] = None
    tts_backend_label = "disabled"
    if tg_tts_enabled:
        backend = (tg_tts_backend or "minimax").strip().lower()
        if backend == "local-gpt-sovits":
            if tg_tts_gsv_root and tg_tts_ref_audio_path:
                try:
                    tts_synthesizer = LocalGptSovitsTtsSynthesizer(
                        root_dir=tg_tts_gsv_root,
                        api_base=tg_tts_api_base,
                        ref_audio_path=tg_tts_ref_audio_path,
                        prompt_text=tg_tts_prompt_text,
                        prompt_lang=tg_tts_prompt_lang,
                        text_lang=tg_tts_text_lang,
                        gpt_weights_path=tg_tts_gpt_weights,
                        sovits_weights_path=tg_tts_sovits_weights,
                        python_bin=tg_tts_python_bin,
                        ffmpeg_bin=tg_tts_ffmpeg_bin,
                        tts_config_path=tg_tts_tts_config,
                        startup_timeout_sec=tg_tts_startup_timeout_sec,
                        request_timeout_sec=tg_tts_request_timeout_sec,
                        speed_factor=tg_tts_speed_factor,
                        max_chars=tg_tts_max_chars,
                    )
                    tts_synthesizer.validate_environment()
                    tts_backend_label = (
                        f"local-gpt-sovits:{tg_tts_api_base} "
                        f"mode={tg_tts_mode or 'auto'} max_chars={tg_tts_max_chars}"
                    )
                except Exception as e:
                    tts_backend_label = f"local-gpt-sovits-unavailable:{e}"
            else:
                tts_backend_label = "local-gpt-sovits-missing-root-or-ref"
        elif backend == "minimax":
            tts_backend_label = (
                f"minimax:{tg_tts_api_base} "
                f"model={tg_tts_model_default or DEFAULT_MINIMAX_MODEL} mode={tg_tts_mode or 'auto'}"
            )
        else:
            tts_backend_label = f"unsupported-backend:{backend}"
    if codex_dangerous_bypass_level == 1:
        log("[warn] CODEX_DANGEROUS_BYPASS=1, enabling sandbox_mode=danger-full-access and approval_policy=never")
    elif codex_dangerous_bypass_level >= 2:
        log("[warn] CODEX_DANGEROUS_BYPASS=2, approvals and sandbox are fully bypassed")
    if tg_stream_enabled:
        log(
            "[info] TG streaming enabled "
            f"(edit interval: {tg_stream_edit_interval_ms}ms, "
            f"min delta: {tg_stream_min_delta_chars}, "
            f"thinking interval: {tg_thinking_status_interval_ms}ms)"
        )
    else:
        log("[info] TG streaming disabled")
    log(f"[info] Telegram replies {'quote source messages' if tg_reply_to_messages else 'send without quote'}")
    log(
        "[info] Telegram prompt time context "
        f"{'enabled' if tg_attach_time_context else 'disabled'} "
        f"(display name: {tg_user_display_name})"
    )
    log(
        "[info] Telegram new-thread persona "
        f"{'enabled' if tg_new_thread_persona_enabled else 'disabled'}"
    )
    log(
        "[info] Telegram memory "
        f"(path: {memory_path}, auto writeback: {'enabled' if memory_auto_enabled else 'disabled'})"
    )
    if not tg_new_thread_persona_prompt:
        log("[info] Telegram new-thread persona prompt is blank by default")
    if not tg_heartbeat_session_prompt:
        log("[info] Telegram heartbeat prompt is blank by default")
    if not tg_memory_context_prompt:
        log("[info] Telegram memory context prompt is blank by default")
    if memory_auto_enabled and not tg_memory_writeback_prompt:
        log("[info] Telegram memory writeback prompt is blank by default; auto writeback will stay idle")
    if codex_idle_timeout_sec > 0:
        log(f"[info] Codex idle timeout enabled ({codex_idle_timeout_sec}s)")
    else:
        log("[warn] Codex idle timeout disabled")
    if tg_voice_enabled and audio_transcriber is not None:
        log(
            "[info] Telegram voice transcription enabled "
            f"(backend: {voice_backend_label}, max bytes: {tg_voice_max_bytes})"
        )
    elif tg_voice_enabled:
        log(f"[warn] Telegram voice transcription requested, but backend is unavailable ({voice_backend_label})")
    else:
        log("[info] Telegram voice transcription disabled")
    if tg_tts_enabled and (tts_synthesizer is not None or (tg_tts_backend or "").strip().lower() == "minimax"):
        log(f"[info] Telegram TTS enabled (backend: {tts_backend_label})")
    elif tg_tts_enabled:
        log(f"[warn] Telegram TTS requested, but backend is unavailable ({tts_backend_label})")
    else:
        log("[info] Telegram TTS disabled")

    return TgCodexService(
        api=api,
        sessions=sessions,
        state=state,
        memory_store=memory_store,
        codex=codex,
        audio_transcriber=audio_transcriber,
        tts_synthesizer=tts_synthesizer,
        default_cwd=default_cwd,
        allowed_user_ids=allowed_user_ids,
        stream_enabled=tg_stream_enabled,
        stream_edit_interval_ms=tg_stream_edit_interval_ms,
        stream_min_delta_chars=tg_stream_min_delta_chars,
        thinking_status_interval_ms=tg_thinking_status_interval_ms,
        reply_to_messages=tg_reply_to_messages,
        attach_time_context=tg_attach_time_context,
        user_display_name=tg_user_display_name,
        new_thread_persona_enabled=tg_new_thread_persona_enabled,
        new_thread_persona_prompt=tg_new_thread_persona_prompt,
        heartbeat_session_prompt=tg_heartbeat_session_prompt,
        heartbeat_banned_patterns=tg_heartbeat_banned_patterns,
        heartbeat_template_messages=tg_heartbeat_template_messages,
        heartbeat_followup_template_messages=tg_heartbeat_followup_template_messages,
        memory_context_prompt=tg_memory_context_prompt,
        memory_writeback_prompt=tg_memory_writeback_prompt,
        memory_auto_enabled=memory_auto_enabled,
        tts_backend=tg_tts_backend,
        tts_mode=tg_tts_mode,
        tts_max_chars=tg_tts_max_chars,
        tts_api_base=tg_tts_api_base,
        tts_default_model=tg_tts_model_default,
        tts_ffmpeg_bin=tg_tts_ffmpeg_bin,
        tts_cache_dir=tg_tts_cache_dir,
    )


def main() -> None:
    ensure_stdio_encoding()
    service = build_service()
    try:
        service.setup_bot_menu()
        log("bot command menu configured")
    except Exception as e:
        log(f"bot command menu setup failed: {e}")
    log("tg-codex service started")
    service.run_forever()


if __name__ == "__main__":
    main()

