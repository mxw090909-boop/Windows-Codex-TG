import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from codex_common import log


DEFAULT_TTS_API_BASE = "http://127.0.0.1:9880"
DEFAULT_TTS_MAX_CHARS = 220
DEFAULT_MINIMAX_API_BASE = "https://api.minimaxi.com/v1"
DEFAULT_MINIMAX_MODEL = "speech-2.8-turbo"
DEFAULT_MINIMAX_LANGUAGE_BOOST = "Chinese"


def _windows_hidden_subprocess_kwargs() -> Dict[str, Any]:
    if os.name != "nt":
        return {}

    kwargs: Dict[str, Any] = {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    if creationflags:
        kwargs["creationflags"] = creationflags

    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is None:
        return kwargs

    try:
        startupinfo = startupinfo_cls()
    except Exception:
        return kwargs

    startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0) or 0)
    startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0) or 0)
    kwargs["startupinfo"] = startupinfo
    return kwargs


@dataclass
class SynthesizedVoiceNote:
    audio_bytes: bytes
    file_name: str = "reply.ogg"
    mime_type: str = "audio/ogg"
    duration_seconds: Optional[int] = None


def derive_prompt_text_from_reference(ref_audio_path: str, explicit_prompt_text: Optional[str] = None) -> str:
    explicit = (explicit_prompt_text or "").strip()
    if explicit:
        return explicit

    stem = Path(ref_audio_path).stem.strip()
    stem = re.sub(r"^\s*[【\[].*?[】\]]\s*", "", stem)
    stem = re.sub(r"^\s*[\(（].*?[\)）]\s*", "", stem)
    return stem.strip()


def is_tts_reply_candidate(text: str, *, mode: str = "auto", max_chars: int = DEFAULT_TTS_MAX_CHARS) -> bool:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    normalized_mode = (mode or "auto").strip().lower()

    if normalized_mode in {"0", "off", "disabled", "never"}:
        return False
    if not cleaned:
        return False
    if len(cleaned) > max(40, max_chars):
        return False
    if normalized_mode in {"1", "on", "always"}:
        return True

    blocked_patterns = [
        "```",
        "stderr:",
        "Traceback",
        "/memory",
        "/use ",
        "/sessions",
        "/status",
        "http://",
        "https://",
    ]
    if any(pattern in cleaned for pattern in blocked_patterns):
        return False
    if re.search(r"`[^`]+`", cleaned):
        return False
    if re.search(r"[A-Za-z]:\\", cleaned):
        return False
    if len([line for line in (text or "").splitlines() if line.strip()]) > 4:
        return False
    return True


def resolve_ffmpeg_bin(explicit_ffmpeg_bin: Optional[str] = None) -> Optional[str]:
    if explicit_ffmpeg_bin:
        explicit = Path(explicit_ffmpeg_bin).expanduser()
        if explicit.exists():
            return str(explicit.resolve())

    discovered = shutil.which("ffmpeg")
    if discovered:
        return discovered

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def convert_audio_bytes_to_voice_note(
    audio_bytes: bytes,
    *,
    input_suffix: str,
    ffmpeg_bin: str,
    temp_dir: Optional[Path] = None,
) -> bytes:
    target_dir = temp_dir or Path(tempfile.gettempdir())
    target_dir.mkdir(parents=True, exist_ok=True)

    fd_in, input_path = tempfile.mkstemp(prefix="tg-tts-", suffix=input_suffix, dir=target_dir)
    os.close(fd_in)
    fd_out, output_path = tempfile.mkstemp(prefix="tg-tts-", suffix=".ogg", dir=target_dir)
    os.close(fd_out)

    try:
        Path(input_path).write_bytes(audio_bytes)
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i",
            input_path,
            "-c:a",
            "libopus",
            "-b:a",
            "48k",
            "-vbr",
            "on",
            "-application",
            "voip",
            output_path,
        ]
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            **_windows_hidden_subprocess_kwargs(),
        )
        if completed.returncode != 0:
            stderr_tail = (completed.stderr or "").strip()[-1200:]
            raise RuntimeError(f"ffmpeg 转 Telegram 语音失败: {stderr_tail}")
        return Path(output_path).read_bytes()
    finally:
        for tmp_path in (input_path, output_path):
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass


class LocalGptSovitsTtsSynthesizer:
    def __init__(
        self,
        *,
        root_dir: str,
        ref_audio_path: str,
        api_base: str = DEFAULT_TTS_API_BASE,
        prompt_text: Optional[str] = None,
        prompt_lang: str = "zh",
        text_lang: str = "zh",
        gpt_weights_path: Optional[str] = None,
        sovits_weights_path: Optional[str] = None,
        python_bin: Optional[str] = None,
        ffmpeg_bin: Optional[str] = None,
        tts_config_path: Optional[str] = None,
        startup_timeout_sec: int = 240,
        request_timeout_sec: int = 240,
        speed_factor: float = 1.0,
        max_chars: int = DEFAULT_TTS_MAX_CHARS,
    ) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.api_base = (api_base or DEFAULT_TTS_API_BASE).rstrip("/")
        parsed = urllib.parse.urlparse(self.api_base)
        self.api_host = parsed.hostname or "127.0.0.1"
        self.api_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.ref_audio_path = self._resolve_path(ref_audio_path, must_exist=True)
        self.prompt_text = derive_prompt_text_from_reference(str(self.ref_audio_path), prompt_text)
        self.prompt_lang = (prompt_lang or "zh").strip().lower() or "zh"
        self.text_lang = (text_lang or "zh").strip().lower() or "zh"
        self.gpt_weights_path = self._resolve_path(gpt_weights_path, must_exist=True) if gpt_weights_path else None
        self.sovits_weights_path = (
            self._resolve_path(sovits_weights_path, must_exist=True) if sovits_weights_path else None
        )
        self.python_bin = self._resolve_python_bin(python_bin)
        self.ffmpeg_bin = resolve_ffmpeg_bin(ffmpeg_bin)
        self.tts_config_path = self._resolve_path(tts_config_path, must_exist=True) if tts_config_path else None
        self.startup_timeout_sec = max(30, int(startup_timeout_sec))
        self.request_timeout_sec = max(30, int(request_timeout_sec))
        self.speed_factor = max(0.5, min(float(speed_factor), 2.5))
        self.max_chars = max(40, int(max_chars))
        self._server_lock = threading.Lock()
        self._synthesis_lock = threading.Lock()
        self._api_process: Optional[subprocess.Popen] = None
        self._process_log_handle = None
        self._weights_applied = False

    def validate_environment(self) -> None:
        if not self.root_dir.exists():
            raise RuntimeError(f"GPT-SoVITS 目录不存在: {self.root_dir}")
        if not (self.root_dir / "api_v2.py").exists():
            raise RuntimeError(f"未找到 api_v2.py: {self.root_dir}")
        if not self.ref_audio_path.exists():
            raise RuntimeError(f"参考音频不存在: {self.ref_audio_path}")
        if self.gpt_weights_path is not None and not self.gpt_weights_path.exists():
            raise RuntimeError(f"GPT 权重不存在: {self.gpt_weights_path}")
        if self.sovits_weights_path is not None and not self.sovits_weights_path.exists():
            raise RuntimeError(f"SoVITS 权重不存在: {self.sovits_weights_path}")
        if not self.python_bin or not Path(self.python_bin).exists():
            raise RuntimeError("未找到 GPT-SoVITS 可用的 Python。")
        if not self.ffmpeg_bin:
            raise RuntimeError("未找到 ffmpeg。")

    def synthesize_voice_note(self, text: str) -> SynthesizedVoiceNote:
        cleaned = re.sub(r"\s+", " ", (text or "").strip())
        if not cleaned:
            raise RuntimeError("要合成的文本是空的。")
        if len(cleaned) > self.max_chars:
            raise RuntimeError(f"文本太长（{len(cleaned)} chars），超过当前上限 {self.max_chars}。")

        with self._synthesis_lock:
            self._ensure_server_ready()
            self._ensure_weights_applied()
            wav_bytes, _ = self._request_audio(
                "/tts",
                {
                    "text": cleaned,
                    "text_lang": self.text_lang,
                    "ref_audio_path": str(self.ref_audio_path),
                    "prompt_lang": self.prompt_lang,
                    "prompt_text": self.prompt_text,
                    "text_split_method": "cut5",
                    "speed_factor": self.speed_factor,
                    "media_type": "wav",
                    "streaming_mode": False,
                    "batch_size": 1,
                    "parallel_infer": True,
                },
            )
            if not wav_bytes:
                raise RuntimeError("GPT-SoVITS 没有返回音频数据。")
            voice_bytes = convert_audio_bytes_to_voice_note(
                wav_bytes,
                input_suffix=".wav",
                ffmpeg_bin=self.ffmpeg_bin,
                temp_dir=self.root_dir / "TEMP",
            )
            return SynthesizedVoiceNote(audio_bytes=voice_bytes)

    def _resolve_path(self, raw_path: str, *, must_exist: bool) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root_dir / candidate
        resolved = candidate.resolve()
        if must_exist and not resolved.exists():
            raise RuntimeError(f"路径不存在: {resolved}")
        return resolved

    def _resolve_python_bin(self, explicit_python_bin: Optional[str]) -> Optional[str]:
        candidates = []
        if explicit_python_bin:
            candidates.append(Path(explicit_python_bin).expanduser())
        candidates.extend(
            [
                self.root_dir / "venv" / "Scripts" / "python.exe",
                self.root_dir / "runtime" / "python.exe",
                self.root_dir / "runtime" / "Scripts" / "python.exe",
            ]
        )
        for candidate in candidates:
            if candidate.exists():
                return str(candidate.resolve())
        return None

    def _ensure_server_ready(self) -> None:
        if self._is_server_ready():
            return

        with self._server_lock:
            if self._is_server_ready():
                return
            if self._api_process is None or self._api_process.poll() is not None:
                self._start_api_process()

            deadline = time.time() + self.startup_timeout_sec
            while time.time() < deadline:
                if self._is_server_ready():
                    return
                if self._api_process is not None and self._api_process.poll() is not None:
                    raise RuntimeError(f"GPT-SoVITS API 提前退出。\n{self._read_process_log_tail()}")
                time.sleep(1.0)

            raise RuntimeError(f"等待 GPT-SoVITS API 超时。\n{self._read_process_log_tail()}")

    def _start_api_process(self) -> None:
        temp_dir = self.root_dir / "TEMP"
        temp_dir.mkdir(parents=True, exist_ok=True)
        log_path = temp_dir / "tg-gsv-api.log"
        if self._process_log_handle is not None:
            try:
                self._process_log_handle.close()
            except Exception:
                pass
            self._process_log_handle = None

        self._process_log_handle = open(log_path, "ab")
        args = [self.python_bin, "api_v2.py", "-a", self.api_host, "-p", str(self.api_port)]
        if self.tts_config_path is not None:
            args.extend(["-c", str(self.tts_config_path)])

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._api_process = subprocess.Popen(
            args,
            cwd=self.root_dir,
            stdout=self._process_log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        self._weights_applied = False
        log(f"started GPT-SoVITS API: pid={self._api_process.pid} host={self.api_host} port={self.api_port}")

    def _read_process_log_tail(self, max_chars: int = 1600) -> str:
        log_path = self.root_dir / "TEMP" / "tg-gsv-api.log"
        if not log_path.exists():
            return "还没有 GPT-SoVITS API 日志。"
        raw = log_path.read_text(encoding="utf-8", errors="ignore")
        return raw[-max_chars:].strip() or "GPT-SoVITS API 日志为空。"

    def _is_server_ready(self) -> bool:
        try:
            req = urllib.request.Request(url=f"{self.api_base}/openapi.json", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False

    def _ensure_weights_applied(self) -> None:
        if self._weights_applied:
            return

        if self.gpt_weights_path is not None:
            self._request_text("/set_gpt_weights", query={"weights_path": self._path_for_api(self.gpt_weights_path)})
        if self.sovits_weights_path is not None:
            self._request_text(
                "/set_sovits_weights",
                query={"weights_path": self._path_for_api(self.sovits_weights_path)},
            )
        self._weights_applied = True

    def _path_for_api(self, path: Path) -> str:
        try:
            rel = path.resolve().relative_to(self.root_dir)
            return rel.as_posix()
        except ValueError:
            return str(path)

    def _request_text(self, path: str, *, query: Optional[Dict[str, Any]] = None) -> str:
        url = self.api_base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url=url, method="GET")
        with urllib.request.urlopen(req, timeout=self.request_timeout_sec) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    def _request_audio(self, path: str, payload: Dict[str, Any]) -> Tuple[bytes, str]:
        req = urllib.request.Request(
            url=self.api_base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout_sec) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"GPT-SoVITS TTS 请求失败: {body or e}") from e


class MiniMaxTtsSynthesizer:
    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str,
        api_base: str = DEFAULT_MINIMAX_API_BASE,
        model: str = DEFAULT_MINIMAX_MODEL,
        ffmpeg_bin: Optional[str] = None,
        cache_dir: Optional[Path] = None,
        request_timeout_sec: int = 120,
        max_chars: int = DEFAULT_TTS_MAX_CHARS,
        language_boost: Optional[str] = DEFAULT_MINIMAX_LANGUAGE_BOOST,
        speed: float = 1.0,
        vol: float = 1.0,
        pitch: int = 0,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.voice_id = (voice_id or "").strip()
        self.api_base = (api_base or DEFAULT_MINIMAX_API_BASE).rstrip("/")
        self.model = (model or DEFAULT_MINIMAX_MODEL).strip()
        self.ffmpeg_bin = resolve_ffmpeg_bin(ffmpeg_bin)
        self.cache_dir = Path(cache_dir).expanduser().resolve() if cache_dir else None
        self.request_timeout_sec = max(30, int(request_timeout_sec))
        self.max_chars = max(40, int(max_chars))
        self.language_boost = (language_boost or "").strip() or None
        self.speed = max(0.5, min(float(speed), 2.0))
        self.vol = max(0.1, min(float(vol), 10.0))
        self.pitch = max(-12, min(int(pitch), 12))
        self._lock = threading.Lock()

    def validate_environment(self) -> None:
        if not self.api_key:
            raise RuntimeError("MiniMax API key 为空。")
        if not self.voice_id:
            raise RuntimeError("MiniMax voice_id 为空。")
        if not self.ffmpeg_bin:
            raise RuntimeError("未找到 ffmpeg。")
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def synthesize_voice_note(self, text: str) -> SynthesizedVoiceNote:
        cleaned = re.sub(r"\s+", " ", (text or "").strip())
        if not cleaned:
            raise RuntimeError("要合成的文本是空的。")
        if len(cleaned) > self.max_chars:
            raise RuntimeError(f"文本太长（{len(cleaned)} chars），超过当前上限 {self.max_chars}。")

        with self._lock:
            cache_path = self._cache_path(cleaned)
            if cache_path is not None and cache_path.exists():
                return SynthesizedVoiceNote(audio_bytes=cache_path.read_bytes())

            mp3_bytes = self._request_mp3(cleaned)
            voice_bytes = convert_audio_bytes_to_voice_note(
                mp3_bytes,
                input_suffix=".mp3",
                ffmpeg_bin=self.ffmpeg_bin,
                temp_dir=self.cache_dir,
            )
            if cache_path is not None:
                cache_path.write_bytes(voice_bytes)
            return SynthesizedVoiceNote(audio_bytes=voice_bytes)

    def _cache_path(self, text: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(
            f"{self.api_base}|{self.model}|{self.voice_id}|{self.speed}|{self.vol}|{self.pitch}|{text}".encode(
                "utf-8"
            )
        ).hexdigest()
        return self.cache_dir / f"{digest}.ogg"

    def _request_mp3(self, text: str) -> bytes:
        payload: Dict[str, Any] = {
            "model": self.model,
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": self.voice_id,
                "speed": self.speed,
                "vol": self.vol,
                "pitch": self.pitch,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 1,
            },
            "output_format": "hex",
            "subtitle_enable": False,
        }
        if self.language_boost:
            payload["language_boost"] = self.language_boost

        req = urllib.request.Request(
            url=f"{self.api_base}/t2a_v2",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"MiniMax TTS 请求失败: HTTP {e.code} {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"MiniMax TTS 请求失败: {e}") from e

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError("MiniMax TTS 返回了无法解析的响应。") from e

        base_resp = parsed.get("base_resp") if isinstance(parsed, dict) else None
        if isinstance(base_resp, dict) and int(base_resp.get("status_code") or 0) != 0:
            raise RuntimeError(
                f"MiniMax TTS 请求失败: {base_resp.get('status_msg') or base_resp.get('status_code')}"
            )

        data = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("MiniMax TTS 没有返回 data。")
        audio_hex = str(data.get("audio") or "").strip()
        if not audio_hex:
            raise RuntimeError("MiniMax TTS 没有返回音频内容。")
        try:
            return bytes.fromhex(audio_hex)
        except ValueError as e:
            raise RuntimeError("MiniMax TTS 返回的音频不是合法 hex。") from e
