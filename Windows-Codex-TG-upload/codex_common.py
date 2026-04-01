#!/usr/bin/env python3
import copy
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union


_STDIO_CONFIGURED = False


def ensure_stdio_encoding() -> None:
    global _STDIO_CONFIGURED
    if _STDIO_CONFIGURED:
        return
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
    _STDIO_CONFIGURED = True


def log(msg: str) -> None:
    ensure_stdio_encoding()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def chunk_text(text: str, size: int = 3800) -> List[str]:
    if len(text) <= size:
        return [text]
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            split_at = text.rfind("\n", start, end)
            if split_at > start:
                end = split_at + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def parse_dangerous_bypass_level(raw: Optional[str]) -> int:
    value = (raw or "0").strip()
    if not value:
        return 0
    try:
        level = int(value)
    except ValueError:
        raise ValueError("CODEX_DANGEROUS_BYPASS must be 0, 1, or 2")
    if level < 0:
        level = 0
    if level > 2:
        level = 2
    return level


def parse_non_negative_int(raw: Optional[str], default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (ValueError, TypeError, AttributeError):
        return default
    return value if value >= 0 else default


def parse_bool_env(raw: Optional[str], default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass
class SessionMeta:
    session_id: str
    timestamp: str
    cwd: str
    file_path: str
    title: str


StateActor = Union[int, str]


class SessionStore:
    def __init__(self, root: Path):
        self.root = root.expanduser()

    def list_recent(self, limit: int = 10) -> List[SessionMeta]:
        if not self.root.exists():
            return []
        files = sorted(self.root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        sessions: List[SessionMeta] = []
        for path in files:
            meta = self._parse_session_meta(path)
            if not meta:
                continue
            sessions.append(meta)
            if len(sessions) >= limit:
                break
        return sessions

    def find_by_id(self, session_id: str) -> Optional[SessionMeta]:
        if not self.root.exists():
            return None
        for path in self.root.rglob("*.jsonl"):
            meta = self._parse_session_meta(path)
            if meta and meta.session_id == session_id:
                return meta
        return None

    def mark_as_desktop_session(self, session_id: str) -> bool:
        meta = self.find_by_id(session_id)
        if not meta:
            return False
        path = Path(meta.file_path)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if not lines:
                return False
            first = json.loads(lines[0])
            if first.get("type") != "session_meta":
                return False
            payload = first.get("payload") or {}
            changed = False
            if payload.get("source") != "vscode":
                payload["source"] = "vscode"
                changed = True
            if payload.get("originator") != "Codex Desktop":
                payload["originator"] = "Codex Desktop"
                changed = True
            if not changed:
                return True
            first["payload"] = payload
            lines[0] = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
        except Exception:
            return False

    def get_history(
        self,
        session_id: str,
        limit: int = 10,
    ) -> Tuple[Optional[SessionMeta], List[Tuple[str, str]]]:
        meta = self.find_by_id(session_id)
        if not meta:
            return None, []
        path = Path(meta.file_path)
        messages: List[Tuple[str, str]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("type") != "event_msg":
                        continue
                    payload = evt.get("payload") or {}
                    msg_type = payload.get("type")
                    if msg_type not in ("user_message", "agent_message"):
                        continue
                    message = (payload.get("message") or "").strip()
                    if not message:
                        continue
                    role = "user" if msg_type == "user_message" else "assistant"
                    messages.append((role, message))
        except Exception:
            return meta, []
        if limit > 0:
            messages = messages[-limit:]
        return meta, messages

    @staticmethod
    def _parse_session_meta(path: Path) -> Optional[SessionMeta]:
        try:
            with path.open("r", encoding="utf-8") as f:
                first_line = f.readline()
            parsed = json.loads(first_line)
            payload = parsed.get("payload") or {}
            if parsed.get("type") != "session_meta":
                return None
            session_id = payload.get("id")
            if not session_id:
                return None
            title = SessionStore._extract_title(path)
            return SessionMeta(
                session_id=session_id,
                timestamp=payload.get("timestamp", "unknown"),
                cwd=payload.get("cwd", "unknown"),
                file_path=str(path),
                title=title or f"session {session_id[:8]}",
            )
        except Exception:
            return None

    @staticmethod
    def _extract_title(path: Path) -> Optional[str]:
        try:
            with path.open("r", encoding="utf-8") as f:
                for _ in range(240):
                    line = f.readline()
                    if not line:
                        break
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("type") != "event_msg":
                        continue
                    payload = evt.get("payload") or {}
                    if payload.get("type") != "user_message":
                        continue
                    message = (payload.get("message") or "").strip()
                    if not message:
                        continue
                    return SessionStore._compact_title(message)
        except Exception:
            return None
        return None

    @staticmethod
    def _compact_title(text: str, limit: int = 46) -> str:
        one_line = " ".join(text.split())
        if len(one_line) <= limit:
            return one_line
        return one_line[: limit - 1] + "…"

    @staticmethod
    def compact_message(text: str, limit: int = 320) -> str:
        one_line = " ".join(text.split())
        if len(one_line) <= limit:
            return one_line
        return one_line[: limit - 1] + "…"


class MemoryStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: Dict[str, Any] = {"users": {}}
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {"users": {}}

    def _save_unlocked(self) -> None:
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()

    def _get_user_unlocked(self, user_id: StateActor) -> Dict[str, Any]:
        users = self.data.setdefault("users", {})
        key = str(user_id)
        if key not in users:
            users[key] = {"memories": []}
        user_data = users[key]
        if not isinstance(user_data.get("memories"), list):
            user_data["memories"] = []
        return user_data

    @staticmethod
    def _normalize_text(text: Any) -> str:
        return " ".join(str(text or "").split()).strip()

    @staticmethod
    def _normalize_tag(tag: Any) -> str:
        cleaned = " ".join(str(tag or "").strip().lower().split())
        return cleaned[:32]

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        cleaned = MemoryStore._normalize_text(text).lower()
        if not cleaned:
            return []
        raw_tokens = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", cleaned)
        seen: Set[str] = set()
        tokens: List[str] = []
        for token in raw_tokens:
            if token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        return tokens

    def _list_memories_unlocked(self, user_id: StateActor) -> List[Dict[str, Any]]:
        user_data = self._get_user_unlocked(user_id)
        values = user_data.get("memories")
        if not isinstance(values, list):
            user_data["memories"] = []
            return user_data["memories"]
        filtered = [item for item in values if isinstance(item, dict)]
        if len(filtered) != len(values):
            user_data["memories"] = filtered
            return filtered
        return values

    @staticmethod
    def _sort_key(item: Dict[str, Any]) -> Tuple[int, int]:
        pinned = 1 if item.get("pinned") else 0
        updated_at = int(item.get("updated_at") or item.get("created_at") or 0)
        return (pinned, updated_at)

    def list_memories(self, user_id: StateActor, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = [copy.deepcopy(item) for item in self._list_memories_unlocked(user_id)]
        items.sort(key=self._sort_key, reverse=True)
        if limit is not None and limit > 0:
            items = items[:limit]
        return items

    def get_memory(self, user_id: StateActor, memory_id: str) -> Optional[Dict[str, Any]]:
        target_id = str(memory_id or "").strip()
        if not target_id:
            return None
        with self._lock:
            for item in self._list_memories_unlocked(user_id):
                if str(item.get("id") or "") == target_id:
                    return copy.deepcopy(item)
        return None

    def add_memory(
        self,
        user_id: StateActor,
        text: str,
        *,
        tags: Optional[List[str]] = None,
        category: str = "general",
        pinned: bool = False,
        source: str = "auto",
        created_at: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        normalized_text = self._normalize_text(text)
        if not normalized_text:
            return None
        timestamp = int(created_at if created_at is not None else time.time())
        normalized_tags = [tag for tag in (self._normalize_tag(tag) for tag in (tags or [])) if tag]
        seen_tags: Set[str] = set()
        unique_tags: List[str] = []
        for tag in normalized_tags:
            if tag in seen_tags:
                continue
            seen_tags.add(tag)
            unique_tags.append(tag)
        normalized_category = self._normalize_text(category).lower() or "general"
        normalized_source = self._normalize_text(source).lower() or "auto"

        with self._lock:
            memories = self._list_memories_unlocked(user_id)
            existing: Optional[Dict[str, Any]] = None
            for item in memories:
                if self._normalize_text(item.get("text")) == normalized_text:
                    existing = item
                    break
            if existing is not None:
                existing["updated_at"] = timestamp
                existing["source"] = normalized_source or existing.get("source") or "auto"
                existing["category"] = normalized_category or existing.get("category") or "general"
                existing["pinned"] = bool(existing.get("pinned") or pinned)
                merged_tags = list(existing.get("tags") or [])
                for tag in unique_tags:
                    if tag not in merged_tags:
                        merged_tags.append(tag)
                existing["tags"] = merged_tags
                self._save_unlocked()
                return copy.deepcopy(existing)

            record = {
                "id": uuid.uuid4().hex[:8],
                "text": normalized_text,
                "tags": unique_tags,
                "category": normalized_category,
                "pinned": bool(pinned),
                "source": normalized_source,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            memories.append(record)
            self._save_unlocked()
            return copy.deepcopy(record)

    def delete_memory(self, user_id: StateActor, memory_id: str) -> bool:
        target_id = str(memory_id or "").strip()
        if not target_id:
            return False
        with self._lock:
            memories = self._list_memories_unlocked(user_id)
            kept = [item for item in memories if str(item.get("id") or "") != target_id]
            if len(kept) == len(memories):
                return False
            user_data = self._get_user_unlocked(user_id)
            user_data["memories"] = kept
            self._save_unlocked()
            return True

    def set_pinned(self, user_id: StateActor, memory_id: str, pinned: bool) -> Optional[Dict[str, Any]]:
        target_id = str(memory_id or "").strip()
        if not target_id:
            return None
        with self._lock:
            for item in self._list_memories_unlocked(user_id):
                if str(item.get("id") or "") != target_id:
                    continue
                item["pinned"] = bool(pinned)
                item["updated_at"] = int(time.time())
                self._save_unlocked()
                return copy.deepcopy(item)
        return None

    def search_memories(self, user_id: StateActor, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        query_text = self._normalize_text(query).lower()
        query_tokens = self._tokenize(query_text)
        if not query_text and not query_tokens:
            return []

        ranked: List[Tuple[int, Dict[str, Any]]] = []
        now_ts = int(time.time())
        with self._lock:
            memories = [copy.deepcopy(item) for item in self._list_memories_unlocked(user_id)]

        for item in memories:
            text = self._normalize_text(item.get("text")).lower()
            tags = [self._normalize_tag(tag) for tag in item.get("tags") or []]
            score = 0
            if item.get("pinned"):
                score += 40
            if query_text and query_text in text:
                score += 24
            for token in query_tokens:
                if token in text:
                    score += 8
                if token in tags:
                    score += 10
            if not score:
                continue
            updated_at = int(item.get("updated_at") or item.get("created_at") or 0)
            age_days = max(0, (now_ts - updated_at) // 86400) if updated_at else 9999
            score += max(0, 6 - min(age_days, 6))
            ranked.append((score, item))

        ranked.sort(key=lambda pair: (pair[0], self._sort_key(pair[1])), reverse=True)
        items = [item for _, item in ranked]
        if limit > 0:
            items = items[:limit]
        return items


class BotState:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: Dict[str, Any] = {"users": {}}
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {"users": {}}

    @staticmethod
    def _normalize_session_id(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _save_unlocked(self) -> None:
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()

    def _get_user_unlocked(self, user_id: StateActor) -> Dict[str, Any]:
        users = self.data.setdefault("users", {})
        key = str(user_id)
        if key not in users:
            users[key] = {}
        return users[key]

    def set_active_session(self, user_id: StateActor, session_id: str, cwd: str) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["active_session_id"] = session_id
            user_data["active_cwd"] = cwd
            self._save_unlocked()

    def clear_active_session(self, user_id: StateActor, cwd: str) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["active_session_id"] = None
            user_data["active_cwd"] = cwd
            self._save_unlocked()

    def get_active(self, user_id: StateActor) -> Tuple[Optional[str], Optional[str]]:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            session_id = self._normalize_session_id(user_data.get("active_session_id"))
            cwd = str(user_data.get("active_cwd") or "").strip() or None
            return session_id, cwd

    def set_last_session_ids(self, user_id: StateActor, session_ids: List[str]) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["last_session_ids"] = session_ids
            self._save_unlocked()

    def get_last_session_ids(self, user_id: StateActor) -> List[str]:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            values = user_data.get("last_session_ids")
            if not isinstance(values, list):
                return []
            return [str(v) for v in values]

    def touch_user(self, user_id: StateActor, chat_id: int, at: Optional[int] = None) -> None:
        timestamp = int(at if at is not None else time.time())
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["last_chat_id"] = int(chat_id)
            user_data["last_user_message_at"] = timestamp
            user_data["last_interaction_at"] = timestamp
            heartbeat = user_data.setdefault("heartbeat", {})
            heartbeat["unanswered_count"] = 0
            heartbeat["awaiting_reply"] = False
            heartbeat["last_reply_at"] = timestamp
            heartbeat["not_before_at"] = timestamp
            self._save_unlocked()

    def touch_assistant(self, user_id: StateActor, chat_id: int, at: Optional[int] = None) -> None:
        timestamp = int(at if at is not None else time.time())
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["last_chat_id"] = int(chat_id)
            user_data["last_assistant_message_at"] = timestamp
            user_data["last_interaction_at"] = timestamp
            self._save_unlocked()

    def configure_heartbeat(
        self,
        user_id: StateActor,
        *,
        enabled: Optional[bool] = None,
        interval_sec: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            heartbeat = user_data.setdefault("heartbeat", {})
            if enabled is not None:
                heartbeat["enabled"] = bool(enabled)
            if interval_sec is not None:
                heartbeat["interval_sec"] = max(60, int(interval_sec))
            self._save_unlocked()
            return copy.deepcopy(heartbeat)

    def get_heartbeat(self, user_id: StateActor) -> Dict[str, Any]:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            heartbeat = user_data.get("heartbeat")
            if not isinstance(heartbeat, dict):
                return {}
            return copy.deepcopy(heartbeat)

    def mark_heartbeat_sent(
        self,
        user_id: StateActor,
        chat_id: int,
        *,
        at: Optional[int] = None,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> None:
        timestamp = int(at if at is not None else time.time())
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["last_chat_id"] = int(chat_id)
            user_data["last_assistant_message_at"] = timestamp
            user_data["last_interaction_at"] = timestamp
            heartbeat = user_data.setdefault("heartbeat", {})
            heartbeat["last_heartbeat_at"] = timestamp
            heartbeat["last_check_at"] = timestamp
            heartbeat["awaiting_reply"] = True
            heartbeat["unanswered_count"] = int(heartbeat.get("unanswered_count") or 0) + 1
            if session_id:
                heartbeat["context_session_id"] = str(session_id)
            if cwd:
                heartbeat["context_cwd"] = str(cwd)
            self._save_unlocked()

    def mark_heartbeat_skipped(self, user_id: StateActor, *, at: Optional[int] = None) -> None:
        timestamp = int(at if at is not None else time.time())
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            heartbeat = user_data.setdefault("heartbeat", {})
            heartbeat["last_check_at"] = timestamp
            self._save_unlocked()

    def set_heartbeat_context(self, user_id: StateActor, session_id: str, cwd: str) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            heartbeat = user_data.setdefault("heartbeat", {})
            heartbeat["context_session_id"] = str(session_id)
            heartbeat["context_cwd"] = str(cwd)
            self._save_unlocked()

    def get_heartbeat_context(self, user_id: StateActor) -> Tuple[Optional[str], Optional[str]]:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            heartbeat = user_data.get("heartbeat")
            if not isinstance(heartbeat, dict):
                return None, None
            session_id = self._normalize_session_id(heartbeat.get("context_session_id"))
            cwd = str(heartbeat.get("context_cwd") or "").strip() or None
            return session_id, cwd

    def set_heartbeat_not_before(self, user_id: StateActor, not_before_at: int) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            heartbeat = user_data.setdefault("heartbeat", {})
            heartbeat["not_before_at"] = int(not_before_at)
            self._save_unlocked()

    def list_users_snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            users = self.data.get("users", {})
            if not isinstance(users, dict):
                return {}
            return copy.deepcopy(users)

    def set_pending_session_pick(self, user_id: StateActor, enabled: bool) -> None:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["pending_session_pick"] = bool(enabled)
            self._save_unlocked()

    def is_pending_session_pick(self, user_id: StateActor) -> bool:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            return bool(user_data.get("pending_session_pick"))

    def update_active_session_if_unchanged(
        self,
        user_id: StateActor,
        expected_session_id: Optional[str],
        next_session_id: str,
        cwd: str,
    ) -> bool:
        with self._lock:
            user_data = self._get_user_unlocked(user_id)
            current_session_id = self._normalize_session_id(user_data.get("active_session_id"))
            if current_session_id != self._normalize_session_id(expected_session_id):
                return False
            user_data["active_session_id"] = next_session_id
            user_data["active_cwd"] = cwd
            self._save_unlocked()
            return True


class RunningPromptRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._running_counts: Dict[str, int] = {}
        self._running_sessions: Dict[str, Set[str]] = {}

    @staticmethod
    def _actor_key(actor: StateActor) -> str:
        return str(actor)

    def try_start(self, actor: StateActor, session_id: Optional[str]) -> bool:
        actor_key = self._actor_key(actor)
        normalized_session_id = BotState._normalize_session_id(session_id)
        with self._lock:
            if normalized_session_id:
                sessions = self._running_sessions.setdefault(actor_key, set())
                if normalized_session_id in sessions:
                    return False
                sessions.add(normalized_session_id)
            self._running_counts[actor_key] = self._running_counts.get(actor_key, 0) + 1
            return True

    def finish(self, actor: StateActor, session_id: Optional[str]) -> None:
        actor_key = self._actor_key(actor)
        normalized_session_id = BotState._normalize_session_id(session_id)
        with self._lock:
            current_count = self._running_counts.get(actor_key, 0)
            if current_count <= 1:
                self._running_counts.pop(actor_key, None)
            elif current_count > 1:
                self._running_counts[actor_key] = current_count - 1

            if normalized_session_id:
                sessions = self._running_sessions.get(actor_key)
                if sessions is not None:
                    sessions.discard(normalized_session_id)
                    if not sessions:
                        self._running_sessions.pop(actor_key, None)

    def count(self, actor: StateActor) -> int:
        actor_key = self._actor_key(actor)
        with self._lock:
            return self._running_counts.get(actor_key, 0)


class CodexRunner:
    def __init__(
        self,
        codex_bin: str,
        sandbox_mode: Optional[str] = None,
        approval_policy: Optional[str] = None,
        dangerous_bypass_level: int = 0,
        idle_timeout_sec: int = 3600,
    ):
        self.codex_bin = codex_bin
        self.sandbox_mode = sandbox_mode
        self.approval_policy = approval_policy
        self.dangerous_bypass_level = max(0, min(2, int(dangerous_bypass_level)))
        self.idle_timeout_sec = max(0, int(idle_timeout_sec))

    @staticmethod
    def _to_toml_string(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _terminate_process_tree(proc: subprocess.Popen[str], force: bool = False) -> None:
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(proc.pid, sig)
            return
        except Exception:
            pass
        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
        except Exception:
            pass

    @staticmethod
    def _close_process_pipes(proc: subprocess.Popen[str]) -> None:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is None:
                continue
            try:
                pipe.close()
            except Exception:
                pass

    @staticmethod
    def _windows_hidden_popen_kwargs() -> Dict[str, Any]:
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

    def run_prompt(
        self,
        prompt: str,
        cwd: Path,
        session_id: Optional[str] = None,
        on_update: Optional[Callable[[str], None]] = None,
        image_paths: Optional[List[Path]] = None,
        ephemeral: bool = False,
    ) -> Tuple[Optional[str], str, str, int]:
        config_flags: List[str] = []
        if self.dangerous_bypass_level == 1:
            sandbox_mode = self.sandbox_mode or "danger-full-access"
            approval_policy = self.approval_policy or "never"
            config_flags.extend(["-c", f"sandbox_mode={self._to_toml_string(sandbox_mode)}"])
            config_flags.extend(["-c", f"approval_policy={self._to_toml_string(approval_policy)}"])

        exec_flags: List[str] = ["--json", "--skip-git-repo-check"]
        if self.dangerous_bypass_level >= 2:
            exec_flags.append("--dangerously-bypass-approvals-and-sandbox")
        if ephemeral:
            exec_flags.append("--ephemeral")
        image_flags: List[str] = []
        for image_path in image_paths or []:
            image_flags.extend(["-i", str(Path(image_path).resolve())])

        if session_id:
            cmd = [
                self.codex_bin,
                "exec",
                "resume",
                *config_flags,
                *exec_flags,
                *image_flags,
                session_id,
                prompt,
            ]
        else:
            cmd = [
                self.codex_bin,
                "exec",
                *config_flags,
                *exec_flags,
                *image_flags,
                prompt,
            ]

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
                **self._windows_hidden_popen_kwargs(),
            )
        except FileNotFoundError as e:
            return None, f"找不到 codex 可执行文件: {self.codex_bin}", str(e), 127

        stdout_lines: List[str] = []
        stderr_chunks: List[str] = []
        activity_lock = threading.Lock()
        last_output_at = [time.monotonic()]

        def mark_output() -> None:
            with activity_lock:
                last_output_at[0] = time.monotonic()

        def _collect_stderr() -> None:
            if proc.stderr is None:
                return
            try:
                for line in proc.stderr:
                    mark_output()
                    stderr_chunks.append(line)
            except Exception:
                return

        stderr_thread: Optional[threading.Thread] = None
        if proc.stderr is not None:
            stderr_thread = threading.Thread(target=_collect_stderr, daemon=True)
            stderr_thread.start()

        timed_out = threading.Event()

        def _watchdog() -> None:
            if self.idle_timeout_sec <= 0:
                return
            while proc.poll() is None:
                time.sleep(5)
                with activity_lock:
                    idle_for_sec = time.monotonic() - last_output_at[0]
                if idle_for_sec < self.idle_timeout_sec:
                    continue
                timed_out.set()
                log(
                    "codex exec idle timed out: "
                    f"pid={proc.pid} idle_timeout_sec={self.idle_timeout_sec} "
                    f"idle_for_sec={int(idle_for_sec)} cwd={cwd}"
                )
                try:
                    self._terminate_process_tree(proc, force=False)
                    proc.wait(timeout=5)
                    self._close_process_pipes(proc)
                    return
                except subprocess.TimeoutExpired:
                    pass
                except Exception:
                    return
                try:
                    self._terminate_process_tree(proc, force=True)
                    proc.wait(timeout=2)
                except Exception:
                    return
                finally:
                    self._close_process_pipes(proc)
                return

        watchdog_thread: Optional[threading.Thread] = None
        if self.idle_timeout_sec > 0:
            watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
            watchdog_thread.start()

        thread_id: Optional[str] = None
        messages: List[str] = []
        current_agent_text = ""
        last_emitted = ""

        if proc.stdout is not None:
            try:
                for raw_line in proc.stdout:
                    mark_output()
                    stdout_lines.append(raw_line.rstrip("\n"))
                    line = raw_line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    evt_thread_id, messages, current_agent_text, changed = self._consume_exec_event(
                        evt,
                        messages,
                        current_agent_text,
                    )
                    if evt_thread_id and not thread_id:
                        thread_id = evt_thread_id
                    if on_update and changed:
                        live_text = self._compose_agent_text(messages, current_agent_text)
                        if live_text and live_text != last_emitted:
                            try:
                                on_update(live_text)
                            except Exception:
                                pass
                            last_emitted = live_text
            except Exception:
                pass

        return_code = proc.wait()
        if watchdog_thread is not None:
            watchdog_thread.join(timeout=0.2)
        if stderr_thread is not None:
            stderr_thread.join(timeout=2.0)
        stderr_text = "".join(stderr_chunks).strip()

        if current_agent_text.strip():
            final_piece = current_agent_text.strip()
            if not messages or messages[-1] != final_piece:
                messages.append(final_piece)

        agent_text = self._compose_agent_text(messages, "")
        stdout_text = "\n".join(stdout_lines)
        if not thread_id or not agent_text:
            parsed_thread_id, parsed_text = self._parse_exec_json(stdout_text)
            if not thread_id:
                thread_id = parsed_thread_id
            if not agent_text:
                agent_text = parsed_text
        if not agent_text:
            merged = (stdout_text + "\n" + stderr_text).strip()
            if merged:
                agent_text = merged[-3500:]
            else:
                agent_text = "Codex 没有返回可展示内容。"
        if timed_out.is_set():
            timeout_text = (
                f"Codex 长时间无输出（>{self.idle_timeout_sec}s），"
                "进程已被终止。通常是卡在外部命令、网络请求、远端连接或等待输入。"
            )
            if agent_text and agent_text != "Codex 没有返回可展示内容。":
                agent_text = f"{timeout_text}\n\n{agent_text}"
            else:
                agent_text = timeout_text
        return thread_id, agent_text, stderr_text, return_code

    @staticmethod
    def _parse_exec_json(stdout: str) -> Tuple[Optional[str], str]:
        thread_id: Optional[str] = None
        messages: List[str] = []
        current_agent_text = ""
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt_thread_id, messages, current_agent_text, _ = CodexRunner._consume_exec_event(
                evt,
                messages,
                current_agent_text,
            )
            if evt_thread_id and not thread_id:
                thread_id = evt_thread_id
        text = CodexRunner._compose_agent_text(messages, current_agent_text)
        return thread_id, text

    @staticmethod
    def _compose_agent_text(messages: List[str], current_agent_text: str) -> str:
        parts = [m.strip() for m in messages if isinstance(m, str) and m.strip()]
        if current_agent_text.strip():
            parts.append(current_agent_text.strip())
        return "\n\n".join(parts).strip()

    @staticmethod
    def _consume_exec_event(
        evt: Dict[str, Any],
        messages: List[str],
        current_agent_text: str,
    ) -> Tuple[Optional[str], List[str], str, bool]:
        thread_id: Optional[str] = None
        changed = False
        event_type = str(evt.get("type") or "").strip().lower()

        if event_type == "thread.started":
            thread_id = str(evt.get("thread_id") or "").strip() or None
            if not thread_id:
                thread = evt.get("thread")
                if isinstance(thread, dict):
                    thread_id = str(thread.get("id") or "").strip() or None

        item = evt.get("item") if isinstance(evt.get("item"), dict) else {}
        item_type = str(item.get("type") or "").strip().lower()
        is_agent_item = item_type in ("agent_message", "assistant_message")

        if event_type in ("item.delta", "response.output_text.delta", "assistant_message.delta", "message.delta"):
            delta = (
                CodexRunner._extract_text_fragment(evt.get("delta"))
                or CodexRunner._extract_text_fragment(evt.get("text_delta"))
                or CodexRunner._extract_text_fragment(evt.get("text"))
                or CodexRunner._extract_text_fragment(item.get("delta"))
                or CodexRunner._extract_text_fragment(item.get("text_delta"))
            )
            if delta:
                if not current_agent_text:
                    current_agent_text = delta
                elif delta.startswith(current_agent_text):
                    current_agent_text = delta
                elif not current_agent_text.endswith(delta):
                    current_agent_text += delta
                changed = True

        if event_type in ("item.updated", "item.completed") and is_agent_item:
            full_text = (
                CodexRunner._extract_text_fragment(item.get("text"))
                or CodexRunner._extract_text_fragment(item.get("content"))
                or CodexRunner._extract_text_fragment(item.get("message"))
            ).strip()
            if full_text:
                current_agent_text = full_text
                changed = True
            if event_type == "item.completed" and current_agent_text.strip():
                finalized = current_agent_text.strip()
                if not messages or messages[-1] != finalized:
                    messages.append(finalized)
                    changed = True
                current_agent_text = ""

        if event_type in ("turn.completed", "response.completed", "thread.completed"):
            fallback_text = (
                CodexRunner._extract_text_fragment(evt.get("output_text"))
                or CodexRunner._extract_text_fragment(evt.get("text"))
            ).strip()
            if fallback_text and (not messages or messages[-1] != fallback_text):
                messages.append(fallback_text)
                changed = True
            if current_agent_text.strip():
                finalized = current_agent_text.strip()
                if not messages or messages[-1] != finalized:
                    messages.append(finalized)
                    changed = True
                current_agent_text = ""

        return thread_id, messages, current_agent_text, changed

    @staticmethod
    def _extract_text_fragment(node: Any) -> str:
        if node is None:
            return ""
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return "".join(CodexRunner._extract_text_fragment(x) for x in node)
        if isinstance(node, dict):
            for key in ("text", "delta", "text_delta", "content", "message", "output_text"):
                if key in node:
                    value = CodexRunner._extract_text_fragment(node.get(key))
                    if value:
                        return value
            return "".join(CodexRunner._extract_text_fragment(v) for v in node.values())
        return ""


def resolve_codex_bin(configured: Optional[str]) -> str:
    if configured:
        return configured
    found = shutil.which("codex")
    if found:
        return found
    app_path = "/Applications/Codex.app/Contents/Resources/codex"
    if Path(app_path).exists():
        return app_path
    return "codex"
