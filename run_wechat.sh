#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BOT_SCRIPT="$SCRIPT_DIR/wechat_codex_service.py"
RUNTIME_DIR="$SCRIPT_DIR/.runtime"
WECHAT_RUNTIME_DIR="$RUNTIME_DIR/wechat"
PID_FILE="$WECHAT_RUNTIME_DIR/wechat_bot.pid"
LOG_FILE="$WECHAT_RUNTIME_DIR/wechat_bot.log"
STATE_PATH="$WECHAT_RUNTIME_DIR/wechat_bot_state.json"

WECHAT_ENABLED="${WECHAT_ENABLED:-}"
WECHAT_API_BASE_URL="${WECHAT_API_BASE_URL:-https://ilinkai.weixin.qq.com}"
WECHAT_LOGIN_BOT_TYPE="${WECHAT_LOGIN_BOT_TYPE:-3}"
ALLOWED_WECHAT_USER_IDS="${ALLOWED_WECHAT_USER_IDS:-}"
WECHAT_REQUIRE_ALLOWLIST="${WECHAT_REQUIRE_ALLOWLIST:-1}"
WECHAT_POLL_TIMEOUT_SEC="${WECHAT_POLL_TIMEOUT_SEC:-35}"
WECHAT_SEND_TYPING="${WECHAT_SEND_TYPING:-1}"
DEFAULT_CWD="${DEFAULT_CWD:-$SCRIPT_DIR}"
CODEX_BIN="${CODEX_BIN:-}"
CODEX_SESSION_ROOT="${CODEX_SESSION_ROOT:-$HOME/.codex/sessions}"
CODEX_SANDBOX_MODE="${CODEX_SANDBOX_MODE:-}"
CODEX_APPROVAL_POLICY="${CODEX_APPROVAL_POLICY:-}"
CODEX_DANGEROUS_BYPASS="${CODEX_DANGEROUS_BYPASS:-0}"

ACCOUNT_FILE="$WECHAT_RUNTIME_DIR/account.json"

resolve_codex_bin() {
  if [[ -n "$CODEX_BIN" ]]; then
    return 0
  fi
  if command -v codex >/dev/null 2>&1; then
    CODEX_BIN="$(command -v codex)"
  fi
}

fail_if_not_configured() {
  resolve_codex_bin
  if [[ ! -x "$CODEX_BIN" ]]; then
    echo "[error] CODEX_BIN 不存在或不可执行: $CODEX_BIN"
    exit 1
  fi
  if [[ ! -d "$DEFAULT_CWD" ]]; then
    echo "[error] DEFAULT_CWD 不存在或不是目录: $DEFAULT_CWD"
    exit 1
  fi
}

ensure_dependency() {
  if ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import qrcode
PY
  then
    echo "[info] 安装依赖 qrcode..."
    "$PYTHON_BIN" -m pip install --user qrcode
  fi
}

has_login() {
  [[ -f "$ACCOUNT_FILE" ]] && grep -q '"token"' "$ACCOUNT_FILE" 2>/dev/null
}

print_permission_notice() {
  if [[ "${CODEX_DANGEROUS_BYPASS}" == "0" ]]; then
    echo "[info] 当前 CODEX_DANGEROUS_BYPASS=0（微信不追加权限参数）"
    echo "[info] 如需更完整权限体验，可设置：export CODEX_DANGEROUS_BYPASS=1"
  fi
}

is_enabled() {
  local raw="${WECHAT_ENABLED:-}"
  local lowered=""
  if [[ -z "$raw" ]]; then
    has_login
    return $?
  fi
  lowered="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"
  case "$lowered" in
    0|false|no|off|disable|disabled) return 1 ;;
    *) return 0 ;;
  esac
}

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      return 0
    fi
    rm -f "$PID_FILE"
  fi
  local existing_pid
  existing_pid="$(pgrep -f "$BOT_SCRIPT" 2>/dev/null | head -n 1 || true)"
  if [[ -n "${existing_pid}" ]]; then
    echo "$existing_pid" >"$PID_FILE"
    return 0
  fi
  return 1
}

login() {
  mkdir -p "$WECHAT_RUNTIME_DIR"
  ensure_dependency
  WECHAT_RUNTIME_DIR="$WECHAT_RUNTIME_DIR" \
  WECHAT_API_BASE_URL="$WECHAT_API_BASE_URL" \
  WECHAT_LOGIN_BOT_TYPE="$WECHAT_LOGIN_BOT_TYPE" \
  "$PYTHON_BIN" "$BOT_SCRIPT" login
}

start() {
  fail_if_not_configured
  mkdir -p "$WECHAT_RUNTIME_DIR"
  print_permission_notice

  if ! is_enabled; then
    echo "[info] 微信已被显式关闭，跳过启动"
    exit 0
  fi

  if ! has_login; then
    echo "[error] 尚未完成微信登录。"
    echo "[hint] 先执行: ./run_wechat.sh login"
    exit 1
  fi

  if is_running; then
    echo "[info] 微信服务已运行，PID=$(cat "$PID_FILE")"
    exit 0
  fi

  echo "[info] 启动微信服务..."
  nohup env \
    WECHAT_ENABLED="$WECHAT_ENABLED" \
    WECHAT_API_BASE_URL="$WECHAT_API_BASE_URL" \
    WECHAT_LOGIN_BOT_TYPE="$WECHAT_LOGIN_BOT_TYPE" \
    ALLOWED_WECHAT_USER_IDS="$ALLOWED_WECHAT_USER_IDS" \
    WECHAT_REQUIRE_ALLOWLIST="$WECHAT_REQUIRE_ALLOWLIST" \
    WECHAT_POLL_TIMEOUT_SEC="$WECHAT_POLL_TIMEOUT_SEC" \
    WECHAT_SEND_TYPING="$WECHAT_SEND_TYPING" \
    WECHAT_RUNTIME_DIR="$WECHAT_RUNTIME_DIR" \
    DEFAULT_CWD="$DEFAULT_CWD" \
    CODEX_BIN="$CODEX_BIN" \
    CODEX_SESSION_ROOT="$CODEX_SESSION_ROOT" \
    CODEX_SANDBOX_MODE="$CODEX_SANDBOX_MODE" \
    CODEX_APPROVAL_POLICY="$CODEX_APPROVAL_POLICY" \
    CODEX_DANGEROUS_BYPASS="$CODEX_DANGEROUS_BYPASS" \
    STATE_PATH="$STATE_PATH" \
    "$PYTHON_BIN" -u "$BOT_SCRIPT" >>"$LOG_FILE" 2>&1 &

  local pid=$!
  echo "$pid" >"$PID_FILE"
  sleep 2

  if kill -0 "$pid" >/dev/null 2>&1; then
    echo "[ok] 微信已启动，PID=$pid"
    echo "[ok] 日志: $LOG_FILE"
  else
    rm -f "$PID_FILE"
    echo "[error] 微信启动失败，最近日志："
    tail -n 80 "$LOG_FILE" || true
    exit 1
  fi
}

stop() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    kill "$pid" >/dev/null 2>&1 || true
    rm -f "$PID_FILE"
    echo "[ok] 微信已停止，PID=$pid"
  else
    echo "[info] 微信服务未运行"
  fi
}

status() {
  if is_running; then
    echo "[ok] 微信运行中，PID=$(cat "$PID_FILE")"
  else
    echo "[info] 微信未运行"
  fi
  if has_login; then
    echo "[ok] 已检测到微信登录凭证"
  else
    echo "[info] 尚未检测到微信登录凭证"
  fi
}

logs() {
  mkdir -p "$WECHAT_RUNTIME_DIR"
  touch "$LOG_FILE"
  tail -f "$LOG_FILE"
}

restart() {
  stop
  start
}

usage() {
  cat <<EOF
用法: ./run_wechat.sh [login|start|stop|restart|status|logs]
默认: start

示例：
export WECHAT_ENABLED=0           # 可选；已登录时默认启用，设为 0 可关闭
export ALLOWED_WECHAT_USER_IDS="xxx@im.wechat"
export WECHAT_REQUIRE_ALLOWLIST=1
export WECHAT_API_BASE_URL="https://ilinkai.weixin.qq.com"
export WECHAT_LOGIN_BOT_TYPE=3
export WECHAT_POLL_TIMEOUT_SEC=35
export WECHAT_SEND_TYPING=1

首次使用：
./run_wechat.sh login
./run_wechat.sh start

# Codex command execution policy
# 0: no extra permission args (default)
# 1: defaults to sandbox_mode=danger-full-access + approval_policy=never
# 2: append --dangerously-bypass-approvals-and-sandbox
export CODEX_SANDBOX_MODE=""    # optional override for level=1
export CODEX_APPROVAL_POLICY="" # optional override for level=1
export CODEX_DANGEROUS_BYPASS=0
EOF
}

cmd="${1:-start}"
case "$cmd" in
login) login ;;
start) start ;;
stop) stop ;;
restart) restart ;;
status) status ;;
logs) logs ;;
help|-h|--help) usage ;;
*)
  usage
  exit 1
  ;;
esac
