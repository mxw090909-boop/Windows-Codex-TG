#!/usr/bin/env python3
import base64
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from codex_common import (
    BotState,
    CodexRunner,
    RunningPromptRegistry,
    SessionStore,
    chunk_text,
    env,
    log,
    parse_bool_env,
    parse_dangerous_bypass_level,
    parse_non_negative_int,
    resolve_codex_bin,
)


DEFAULT_WECHAT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_WECHAT_LOGIN_BOT_TYPE = "3"
SESSION_EXPIRED_ERRCODE = -14
MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
MESSAGE_ITEM_TYPE_TEXT = 1
TYPING_STATUS_TYPING = 1
TYPING_STATUS_CANCEL = 2


def parse_allowed_wechat_user_ids(raw: Optional[str]) -> Optional[Set[str]]:
    if not raw:
        return None
    result: Set[str] = set()
    for part in raw.split(","):
        value = part.strip()
        if value:
            result.add(value)
    return result or None


def parse_wechat_enabled(raw: Optional[str], has_login: bool) -> bool:
    if raw is None:
        return has_login
    value = raw.strip().lower()
    if value in {"0", "false", "no", "off", "disable", "disabled"}:
        return False
    if value in {"1", "true", "yes", "on", "enable", "enabled"}:
        return True
    return has_login


class WechatAccountStore:
    def __init__(self, runtime_dir: Path):
        self.runtime_dir = runtime_dir.expanduser()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.account_path = self.runtime_dir / "account.json"
        self.poll_state_path = self.runtime_dir / "poll_state.json"
        self._lock = threading.RLock()

    def load_account(self) -> Dict[str, Any]:
        with self._lock:
            if not self.account_path.exists():
                return {}
            try:
                parsed = json.loads(self.account_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}

    def save_account(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            current = self.load_account()
            current.update(payload)
            current["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.account_path.write_text(
                json.dumps(current, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def has_token(self) -> bool:
        token = str(self.load_account().get("token") or "").strip()
        return bool(token)

    def token(self) -> Optional[str]:
        token = str(self.load_account().get("token") or "").strip()
        return token or None

    def base_url(self) -> str:
        value = str(self.load_account().get("base_url") or "").strip()
        return value or DEFAULT_WECHAT_BASE_URL

    def user_id(self) -> Optional[str]:
        value = str(self.load_account().get("user_id") or "").strip()
        return value or None

    def load_get_updates_buf(self) -> str:
        with self._lock:
            if not self.poll_state_path.exists():
                return ""
            try:
                parsed = json.loads(self.poll_state_path.read_text(encoding="utf-8"))
            except Exception:
                return ""
            if not isinstance(parsed, dict):
                return ""
            value = parsed.get("get_updates_buf")
            return str(value).strip() if value else ""

    def save_get_updates_buf(self, get_updates_buf: str) -> None:
        with self._lock:
            payload = {
                "get_updates_buf": get_updates_buf,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self.poll_state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def clear_get_updates_buf(self) -> None:
        with self._lock:
            if self.poll_state_path.exists():
                self.poll_state_path.unlink()


class WechatAPI:
    def __init__(self, base_url: str, token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.token = token

    @staticmethod
    def _random_wechat_uin() -> str:
        value = random.randint(0, 2**32 - 1)
        return base64.b64encode(str(value).encode("utf-8")).decode("ascii")

    @staticmethod
    def _build_base_info() -> Dict[str, str]:
        return {"channel_version": "tg-codex-wechat/0.1"}

    def _request_json(
        self,
        *,
        endpoint: str,
        payload: Optional[Dict[str, Any]] = None,
        method: str = "POST",
        timeout_sec: int = 30,
        auth: bool = True,
        query: Optional[Dict[str, str]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        headers: Dict[str, str] = {}
        data: Optional[bytes] = None
        if method.upper() == "POST":
            body = payload or {}
            if "base_info" not in body:
                body = {**body, "base_info": self._build_base_info()}
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(data))
        if auth:
            if not self.token:
                raise RuntimeError("missing WeChat bot token")
            headers["AuthorizationType"] = "ilink_bot_token"
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-WECHAT-UIN"] = self._random_wechat_uin()
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url=url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"WeChat API {endpoint} failed: HTTP {e.code} {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"WeChat API {endpoint} failed: {e}") from e
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"WeChat API {endpoint} returned invalid JSON: {raw[:200]}") from e
        if not isinstance(parsed, dict):
            raise RuntimeError(f"WeChat API {endpoint} returned unexpected payload")
        return parsed

    def start_login(self, bot_type: str) -> Dict[str, Any]:
        return self._request_json(
            endpoint="ilink/bot/get_bot_qrcode",
            method="GET",
            query={"bot_type": bot_type},
            timeout_sec=30,
            auth=False,
        )

    def get_qrcode_status(self, qrcode: str) -> Dict[str, Any]:
        return self._request_json(
            endpoint="ilink/bot/get_qrcode_status",
            method="GET",
            query={"qrcode": qrcode},
            timeout_sec=40,
            auth=False,
            extra_headers={"iLink-App-ClientVersion": "1"},
        )

    def get_updates(self, get_updates_buf: str, timeout_sec: int) -> Dict[str, Any]:
        return self._request_json(
            endpoint="ilink/bot/getupdates",
            payload={"get_updates_buf": get_updates_buf or ""},
            timeout_sec=max(5, timeout_sec),
        )

    def send_text(self, to_user_id: str, context_token: str, text: str) -> str:
        client_id = f"tg-codex-wechat-{uuid.uuid4().hex}"
        self._request_json(
            endpoint="ilink/bot/sendmessage",
            payload={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": client_id,
                    "message_type": MESSAGE_TYPE_BOT,
                    "message_state": MESSAGE_STATE_FINISH,
                    "context_token": context_token,
                    "item_list": [
                        {
                            "type": MESSAGE_ITEM_TYPE_TEXT,
                            "text_item": {"text": text},
                        }
                    ],
                }
            },
            timeout_sec=20,
        )
        return client_id

    def get_config(self, ilink_user_id: str, context_token: str) -> Dict[str, Any]:
        return self._request_json(
            endpoint="ilink/bot/getconfig",
            payload={
                "ilink_user_id": ilink_user_id,
                "context_token": context_token,
            },
            timeout_sec=15,
        )

    def send_typing(self, ilink_user_id: str, typing_ticket: str, status: int) -> None:
        self._request_json(
            endpoint="ilink/bot/sendtyping",
            payload={
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "status": status,
            },
            timeout_sec=15,
        )


class WechatTypingStatus:
    def __init__(
        self,
        api: WechatAPI,
        user_id: str,
        context_token: str,
        interval_sec: float = 4.0,
    ):
        self.api = api
        self.user_id = user_id
        self.context_token = context_token
        self.interval_sec = interval_sec
        self._typing_ticket: Optional[str] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _ensure_ticket(self) -> bool:
        if self._typing_ticket:
            return True
        try:
            resp = self.api.get_config(self.user_id, self.context_token)
        except Exception:
            return False
        ticket = str(resp.get("typing_ticket") or "").strip()
        if not ticket:
            return False
        self._typing_ticket = ticket
        return True

    def start(self) -> None:
        if self._thread is not None:
            return
        if not self._ensure_ticket():
            return
        try:
            self.api.send_typing(self.user_id, self._typing_ticket or "", TYPING_STATUS_TYPING)
        except Exception:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        if self._typing_ticket:
            try:
                self.api.send_typing(self.user_id, self._typing_ticket, TYPING_STATUS_CANCEL)
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_sec):
            if not self._typing_ticket:
                return
            try:
                self.api.send_typing(self.user_id, self._typing_ticket, TYPING_STATUS_TYPING)
            except Exception:
                return


def extract_text_from_item_list(item_list: Any) -> str:
    if not isinstance(item_list, list):
        return ""
    for item in item_list:
        if not isinstance(item, dict):
            continue
        if int(item.get("type") or 0) != MESSAGE_ITEM_TYPE_TEXT:
            continue
        text_item = item.get("text_item")
        if not isinstance(text_item, dict):
            continue
        text = str(text_item.get("text") or "").strip()
        if text:
            return text
    return ""


def display_qrcode(qrcode_url: str) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(qrcode_url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        print(qrcode_url)


def login_flow(runtime_dir: Path, api_base_url: str, bot_type: str) -> int:
    store = WechatAccountStore(runtime_dir)
    api = WechatAPI(api_base_url)
    start = api.start_login(bot_type)
    qrcode = str(start.get("qrcode") or "").strip()
    qrcode_url = str(start.get("qrcode_img_content") or "").strip()
    if not qrcode or not qrcode_url:
        print("[error] 微信登录未返回二维码信息。", file=sys.stderr)
        return 1

    print("[info] 请使用微信扫描下面的二维码完成授权：")
    display_qrcode(qrcode_url)
    print(f"[info] 二维码链接: {qrcode_url}")

    started_at = time.time()
    scan_notified = False
    while time.time() - started_at < 8 * 60:
        status = api.get_qrcode_status(qrcode)
        state = str(status.get("status") or "").strip().lower()
        if state == "scaned" and not scan_notified:
            print("[info] 已扫码，请在手机上确认授权。")
            scan_notified = True
        if state == "confirmed":
            token = str(status.get("bot_token") or "").strip()
            account_id = str(status.get("ilink_bot_id") or "").strip()
            user_id = str(status.get("ilink_user_id") or "").strip()
            base_url = str(status.get("baseurl") or "").strip() or api_base_url
            if not token:
                print("[error] 微信登录已确认，但未返回 token。", file=sys.stderr)
                return 1
            store.save_account(
                {
                    "token": token,
                    "account_id": account_id,
                    "user_id": user_id,
                    "base_url": base_url,
                }
            )
            print("[ok] 微信登录成功，凭证已保存。")
            if user_id:
                print(f"[ok] 当前微信账号 user_id: {user_id}")
            return 0
        if state == "expired":
            print("[error] 二维码已过期，请重新执行登录。", file=sys.stderr)
            return 1
        time.sleep(2)

    print("[error] 登录超时，请重新执行登录。", file=sys.stderr)
    return 1


class WechatCodexService:
    def __init__(
        self,
        api: WechatAPI,
        sessions: SessionStore,
        state: BotState,
        codex: CodexRunner,
        default_cwd: Path,
        allowed_user_ids: Optional[Set[str]],
        poll_timeout_sec: int,
        send_typing_enabled: bool,
        account_store: WechatAccountStore,
    ):
        self.api = api
        self.sessions = sessions
        self.state = state
        self.codex = codex
        self.default_cwd = default_cwd
        self.allowed_user_ids = allowed_user_ids
        self.poll_timeout_sec = max(5, poll_timeout_sec)
        self.send_typing_enabled = send_typing_enabled
        self.account_store = account_store
        self.running_prompts = RunningPromptRegistry()
        self.seen_message_ids: Set[str] = set()

    def run_forever(self) -> None:
        get_updates_buf = self.account_store.load_get_updates_buf()
        while True:
            try:
                resp = self.api.get_updates(get_updates_buf, timeout_sec=self.poll_timeout_sec)
                errcode = int(resp.get("errcode") or resp.get("ret") or 0)
                if errcode == SESSION_EXPIRED_ERRCODE:
                    log("[warn] WeChat session expired, clearing poll cursor and retrying")
                    get_updates_buf = ""
                    self.account_store.clear_get_updates_buf()
                    time.sleep(3)
                    continue
                if resp.get("ret") not in (None, 0):
                    raise RuntimeError(f"weixin getupdates failed: {resp}")
                next_buf = str(resp.get("get_updates_buf") or "").strip()
                if next_buf:
                    get_updates_buf = next_buf
                    self.account_store.save_get_updates_buf(next_buf)
                for message in resp.get("msgs") or []:
                    if isinstance(message, dict):
                        self._handle_message(message)
            except Exception as e:
                print(f"[warn] wechat loop error: {e}", file=sys.stderr)
                time.sleep(2)

    @staticmethod
    def _parse_command(text: str) -> Tuple[str, str]:
        parts = text.split(" ", 1)
        cmd = parts[0][1:]
        cmd = cmd.split("@", 1)[0].strip().lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        return cmd, arg

    def _send_text(self, to_user_id: str, context_token: str, text: str) -> None:
        for part in chunk_text(text, size=3500):
            self.api.send_text(to_user_id, context_token, part)

    def _handle_message(self, message: Dict[str, Any]) -> None:
        message_type = int(message.get("message_type") or 0)
        if message_type != MESSAGE_TYPE_USER:
            return
        from_user_id = str(message.get("from_user_id") or "").strip()
        context_token = str(message.get("context_token") or "").strip()
        if not from_user_id or not context_token:
            return

        message_id = str(message.get("message_id") or "").strip()
        if message_id:
            if message_id in self.seen_message_ids:
                return
            self.seen_message_ids.add(message_id)
            if len(self.seen_message_ids) > 10000:
                self.seen_message_ids.clear()

        if self.allowed_user_ids is not None and from_user_id not in self.allowed_user_ids:
            self._send_text(from_user_id, context_token, "没有权限使用这个 bot。")
            return

        text = extract_text_from_item_list(message.get("item_list"))
        if not text:
            return

        log(f"wechat message received: user_id={from_user_id} text={text[:80]!r}")
        if not text.startswith("/"):
            if self._try_handle_quick_session_pick(from_user_id, context_token, text):
                return
            self.state.set_pending_session_pick(from_user_id, False)
            self._run_prompt(from_user_id, context_token, text)
            return

        cmd, arg = self._parse_command(text)
        if cmd in ("start", "help"):
            self._send_help(from_user_id, context_token)
            return
        if cmd == "sessions":
            self._handle_sessions(from_user_id, context_token, arg)
            return
        if cmd == "use":
            self._handle_use(from_user_id, context_token, arg)
            return
        if cmd == "status":
            self._handle_status(from_user_id, context_token)
            return
        if cmd == "new":
            self._handle_new(from_user_id, context_token, arg)
            return
        if cmd == "history":
            self._handle_history(from_user_id, context_token, arg)
            return
        if cmd == "ask":
            self._handle_ask(from_user_id, context_token, arg)
            return
        self._send_text(from_user_id, context_token, f"未知命令: /{cmd}\n发送 /help 查看说明。")

    def _send_help(self, actor_id: str, context_token: str) -> None:
        self._send_text(
            actor_id,
            context_token,
            "\n".join(
                [
                    "可用命令:",
                    "/sessions [N] - 查看最近 N 条会话（标题 + 编号）",
                    "/use <编号|session_id> - 切换当前会话",
                    "/history [编号|session_id] [N] - 查看会话最近 N 条消息",
                    "/new [cwd] - 进入新会话模式（下一条普通消息会新建 session）",
                    "/status - 查看当前绑定会话",
                    "/ask <内容> - 手动提问（可选）",
                    "执行 /sessions 后，可直接发送编号切换会话",
                    "后台执行时仍可发送 /use /sessions /status",
                    "直接发普通消息即可对话（会自动续聊当前 session）",
                ]
            ),
        )

    def _handle_sessions(self, actor_id: str, context_token: str, arg: str) -> None:
        limit = 10
        if arg:
            try:
                limit = max(1, min(30, int(arg)))
            except ValueError:
                self._send_text(actor_id, context_token, "参数错误，示例: /sessions 10")
                return
        items = self.sessions.list_recent(limit=limit)
        if not items:
            self._send_text(actor_id, context_token, "未找到本地会话记录。")
            return
        lines = ["最近会话（用 /use 编号 切换）:"]
        session_ids = [s.session_id for s in items]
        for i, s in enumerate(items, start=1):
            short_id = s.session_id[:8]
            cwd_name = Path(s.cwd).name or s.cwd
            lines.append(f"{i}. {s.title} | {short_id} | {cwd_name}")
        lines.append("直接发送编号即可切换（例如发送: 1）")
        self._send_text(actor_id, context_token, "\n".join(lines))
        self.state.set_last_session_ids(actor_id, session_ids)
        self.state.set_pending_session_pick(actor_id, True)

    def _resolve_session_selector(self, actor_id: str, selector: str) -> Tuple[Optional[str], Optional[str]]:
        raw = selector.strip()
        if not raw:
            return None, "示例: /use 1 或 /use <session_id>"
        if raw.isdigit():
            idx = int(raw)
            recent_ids = self.state.get_last_session_ids(actor_id)
            if idx <= 0 or idx > len(recent_ids):
                return None, "编号无效。先执行 /sessions，再用编号。"
            return recent_ids[idx - 1], None
        return raw, None

    def _switch_to_session(self, actor_id: str, context_token: str, session_id: str) -> None:
        meta = self.sessions.find_by_id(session_id)
        if not meta:
            self._send_text(actor_id, context_token, f"未找到 session: {session_id}")
            return
        self.state.set_active_session(actor_id, meta.session_id, meta.cwd)
        self.state.set_pending_session_pick(actor_id, False)
        self._send_text(
            actor_id,
            context_token,
            f"已切换到:\n{meta.title}\nsession: {meta.session_id}\ncwd: {meta.cwd}\n现在可直接发消息对话。",
        )

    def _handle_use(self, actor_id: str, context_token: str, arg: str) -> None:
        session_id, err = self._resolve_session_selector(actor_id, arg)
        if err:
            self._send_text(actor_id, context_token, err)
            return
        if not session_id:
            self._send_text(actor_id, context_token, "无效的会话选择参数。")
            return
        self._switch_to_session(actor_id, context_token, session_id)

    def _try_handle_quick_session_pick(self, actor_id: str, context_token: str, text: str) -> bool:
        if not self.state.is_pending_session_pick(actor_id):
            return False
        raw = text.strip()
        if not raw.isdigit():
            return False
        idx = int(raw)
        recent_ids = self.state.get_last_session_ids(actor_id)
        if idx <= 0 or idx > len(recent_ids):
            self._send_text(actor_id, context_token, "编号无效。请发送 /sessions 重新查看列表。")
            return True
        self._switch_to_session(actor_id, context_token, recent_ids[idx - 1])
        return True

    def _handle_history(self, actor_id: str, context_token: str, arg: str) -> None:
        tokens = [x for x in arg.split() if x]
        limit = 10
        session_id: Optional[str] = None

        if not tokens:
            session_id, _ = self.state.get_active(actor_id)
            if not session_id:
                self._send_text(
                    actor_id,
                    context_token,
                    "当前无 active session。先 /use 选择会话，或直接对话后再查看历史。",
                )
                return
        else:
            session_id, err = self._resolve_session_selector(actor_id, tokens[0])
            if err:
                self._send_text(actor_id, context_token, err)
                return
            if not session_id:
                self._send_text(actor_id, context_token, "无效的会话选择参数。")
                return
            if len(tokens) >= 2:
                try:
                    limit = int(tokens[1])
                except ValueError:
                    self._send_text(actor_id, context_token, "N 必须是数字，示例: /history 1 20")
                    return

        limit = max(1, min(50, limit))
        meta, messages = self.sessions.get_history(session_id, limit=limit)
        if not meta:
            self._send_text(actor_id, context_token, f"未找到 session: {session_id}")
            return
        if not messages:
            self._send_text(actor_id, context_token, "该会话暂无可展示历史消息。")
            return
        lines = [
            f"会话历史: {meta.title}",
            f"session: {meta.session_id}",
            f"显示最近 {len(messages)} 条消息:",
        ]
        for i, (role, message) in enumerate(messages, start=1):
            role_zh = "用户" if role == "user" else "助手"
            lines.append(f"{i}. [{role_zh}] {SessionStore.compact_message(message)}")
        self._send_text(actor_id, context_token, "\n".join(lines))

    def _handle_status(self, actor_id: str, context_token: str) -> None:
        session_id, cwd = self.state.get_active(actor_id)
        running_count = self.running_prompts.count(actor_id)
        if not session_id:
            message = "当前没有绑定会话。可先 /sessions + /use，或 /new 后直接发消息。"
            if running_count > 0:
                message += f"\n后台仍有 {running_count} 个任务运行，可继续 /use 切线程。"
            self._send_text(actor_id, context_token, message)
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
        self._send_text(actor_id, context_token, "\n".join(lines))

    def _handle_ask(self, actor_id: str, context_token: str, arg: str) -> None:
        prompt = arg.strip()
        if not prompt:
            self._send_text(actor_id, context_token, "示例: /ask 帮我总结当前仓库结构")
            return
        self._run_prompt(actor_id, context_token, prompt)

    def _handle_new(self, actor_id: str, context_token: str, arg: str) -> None:
        cwd_raw = arg.strip()
        _, current_cwd = self.state.get_active(actor_id)
        target_cwd = Path(current_cwd).expanduser() if current_cwd else self.default_cwd
        if cwd_raw:
            candidate = Path(cwd_raw).expanduser()
            if not candidate.exists() or not candidate.is_dir():
                self._send_text(actor_id, context_token, f"cwd 不存在或不是目录: {candidate}")
                return
            target_cwd = candidate
        self.state.clear_active_session(actor_id, str(target_cwd))
        self.state.set_pending_session_pick(actor_id, False)
        self._send_text(
            actor_id,
            context_token,
            f"已进入新会话模式，cwd: {target_cwd}\n下一条普通消息会创建一个新 session。",
        )

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

    @staticmethod
    def _format_prompt_response(session_label: str, text: str) -> str:
        body = (text or "Codex 没有返回可展示内容。").strip() or "Codex 没有返回可展示内容。"
        return body

    def _run_prompt(self, actor_id: str, context_token: str, prompt: str) -> None:
        active_id, active_cwd = self.state.get_active(actor_id)
        cwd = Path(active_cwd).expanduser() if active_cwd else self.default_cwd
        if not cwd.exists():
            cwd = self.default_cwd
        if not self.running_prompts.try_start(actor_id, active_id):
            busy_session = active_id[:8] if active_id else "当前线程"
            self._send_text(
                actor_id,
                context_token,
                f"会话 {busy_session} 已有任务运行中。可先 /use 切到其他线程，或等待当前回复完成。",
            )
            return

        session_label = self._session_label(active_id, cwd)
        mode = "继续当前会话" if active_id else "新建会话"
        log(f"queue wechat prompt: actor={actor_id} mode={mode} cwd={cwd} session={active_id}")
        worker = threading.Thread(
            target=self._run_prompt_worker,
            args=(actor_id, context_token, prompt, active_id, cwd, session_label),
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            self.running_prompts.finish(actor_id, active_id)
            raise

    def _run_prompt_worker(
        self,
        actor_id: str,
        context_token: str,
        prompt: str,
        active_id: Optional[str],
        cwd: Path,
        session_label: str,
    ) -> None:
        typing: Optional[WechatTypingStatus] = None
        run_started_at = time.time()
        if self.send_typing_enabled:
            typing = WechatTypingStatus(self.api, actor_id, context_token)
            typing.start()

        try:
            thread_id, answer, stderr_text, return_code = self.codex.run_prompt(
                prompt=prompt,
                cwd=cwd,
                session_id=active_id,
                on_update=None,
            )
        except Exception as e:
            err_msg = self._format_prompt_response(session_label, f"调用 Codex 时出现异常: {e}")
            self._send_text(actor_id, context_token, err_msg)
            return
        finally:
            if typing is not None:
                typing.stop()
            self.running_prompts.finish(actor_id, active_id)

        elapsed_sec = round(time.time() - run_started_at, 2)
        log(
            "wechat prompt finished: "
            f"actor={actor_id} session={active_id} thread={thread_id} "
            f"exit={return_code} elapsed_sec={elapsed_sec}"
        )

        final_session_id = thread_id or active_id
        final_session_label = self._session_label(final_session_id, cwd)
        session_updated = False
        if thread_id:
            session_updated = self.state.update_active_session_if_unchanged(
                actor_id,
                active_id,
                thread_id,
                str(cwd),
            )

        if return_code != 0:
            msg = f"Codex 执行失败 (exit={return_code})\n{answer}"
            if stderr_text:
                msg += f"\n\nstderr:\n{stderr_text[-1200:]}"
            self._send_text(actor_id, context_token, self._format_prompt_response(final_session_label, msg))
            return

        if thread_id and not session_updated:
            current_active_id, _ = self.state.get_active(actor_id)
            if current_active_id != thread_id:
                note = "当前活动线程未变；这是后台线程的回复。"
                if not active_id:
                    note = "新线程已创建，但你已经切到别的线程，当前活动线程未变。"
                answer = f"{note}\n\n{answer}"

        self._send_text(actor_id, context_token, self._format_prompt_response(final_session_label, answer))


def build_service() -> WechatCodexService:
    api_base_url = env("WECHAT_API_BASE_URL", DEFAULT_WECHAT_BASE_URL) or DEFAULT_WECHAT_BASE_URL
    require_allowlist = parse_bool_env(env("WECHAT_REQUIRE_ALLOWLIST"), True)
    allowed_user_ids = parse_allowed_wechat_user_ids(env("ALLOWED_WECHAT_USER_IDS"))
    runtime_dir = Path(env("WECHAT_RUNTIME_DIR", "./.runtime/wechat")).expanduser()
    store = WechatAccountStore(runtime_dir)
    token = store.token()
    if not token:
        raise RuntimeError("missing WeChat login token, run ./run_wechat.sh login first")
    if not parse_wechat_enabled(env("WECHAT_ENABLED"), has_login=True):
        raise RuntimeError("WeChat channel disabled by WECHAT_ENABLED")
    default_allowed_user_id = store.user_id()
    if require_allowlist and not allowed_user_ids and default_allowed_user_id:
        allowed_user_ids = {default_allowed_user_id}
    if require_allowlist and not allowed_user_ids:
        raise RuntimeError(
            "ALLOWED_WECHAT_USER_IDS is required by default for safety. "
            "Set your WeChat user ID, or set WECHAT_REQUIRE_ALLOWLIST=0 to override."
        )

    state_path = Path(env("STATE_PATH", "./wechat_bot_state.json")).expanduser()
    session_root = Path(env("CODEX_SESSION_ROOT", "~/.codex/sessions")).expanduser()
    codex_bin = resolve_codex_bin(env("CODEX_BIN"))
    codex_sandbox_mode = env("CODEX_SANDBOX_MODE")
    codex_approval_policy = env("CODEX_APPROVAL_POLICY")
    codex_dangerous_bypass_level = parse_dangerous_bypass_level(env("CODEX_DANGEROUS_BYPASS", "0"))
    codex_idle_timeout_sec = parse_non_negative_int(
        env("CODEX_IDLE_TIMEOUT_SEC", env("CODEX_EXEC_TIMEOUT_SEC", "3600")),
        3600,
    )
    poll_timeout_sec = parse_non_negative_int(env("WECHAT_POLL_TIMEOUT_SEC", "35"), 35)
    send_typing_enabled = parse_bool_env(env("WECHAT_SEND_TYPING"), True)
    default_cwd = Path(env("DEFAULT_CWD", os.getcwd())).expanduser()

    if codex_dangerous_bypass_level > 0:
        log(f"warn: CODEX_DANGEROUS_BYPASS={codex_dangerous_bypass_level} expands local machine risk")
    if default_cwd == Path.home() or str(default_cwd) == "/":
        log(f"warn: DEFAULT_CWD points to a broad directory: {default_cwd}")

    api = WechatAPI(store.base_url() or api_base_url, token=token)
    sessions = SessionStore(session_root)
    state = BotState(state_path)
    codex = CodexRunner(
        codex_bin=codex_bin,
        sandbox_mode=codex_sandbox_mode,
        approval_policy=codex_approval_policy,
        dangerous_bypass_level=codex_dangerous_bypass_level,
        idle_timeout_sec=codex_idle_timeout_sec,
    )
    if codex_dangerous_bypass_level == 1:
        log("[warn] CODEX_DANGEROUS_BYPASS=1, enabling sandbox_mode=danger-full-access and approval_policy=never")
    elif codex_dangerous_bypass_level >= 2:
        log("[warn] CODEX_DANGEROUS_BYPASS=2, approvals and sandbox are fully bypassed")
    if codex_idle_timeout_sec > 0:
        log(f"[info] Codex idle timeout enabled ({codex_idle_timeout_sec}s)")
    else:
        log("[warn] Codex idle timeout disabled")
    log(f"[info] WeChat polling enabled (timeout: {poll_timeout_sec}s, typing: {int(send_typing_enabled)})")
    return WechatCodexService(
        api=api,
        sessions=sessions,
        state=state,
        codex=codex,
        default_cwd=default_cwd,
        allowed_user_ids=allowed_user_ids,
        poll_timeout_sec=poll_timeout_sec,
        send_typing_enabled=send_typing_enabled,
        account_store=store,
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    cmd = args[0] if args else "serve"
    if cmd == "login":
        runtime_dir = Path(env("WECHAT_RUNTIME_DIR", "./.runtime/wechat")).expanduser()
        api_base_url = env("WECHAT_API_BASE_URL", DEFAULT_WECHAT_BASE_URL) or DEFAULT_WECHAT_BASE_URL
        bot_type = env("WECHAT_LOGIN_BOT_TYPE", DEFAULT_WECHAT_LOGIN_BOT_TYPE) or DEFAULT_WECHAT_LOGIN_BOT_TYPE
        return login_flow(runtime_dir, api_base_url, bot_type)

    service = build_service()
    log("wechat-codex service started")
    service.run_forever()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
