#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUNTIME_DIR="$SCRIPT_DIR/.runtime"

# Telegram runtime
TG_BOT_SCRIPT="$SCRIPT_DIR/tg_codex_bot.py"
TG_PID_FILE="$RUNTIME_DIR/bot.pid"
TG_LOG_FILE="$RUNTIME_DIR/bot.log"
TG_STATE_PATH="$RUNTIME_DIR/bot_state.json"

# Feishu runtime
FEISHU_RUN_SCRIPT="$SCRIPT_DIR/run_feishu.sh"
FEISHU_LOG_FILE="$RUNTIME_DIR/feishu_bot.log"

# WeChat runtime
WECHAT_RUN_SCRIPT="$SCRIPT_DIR/run_wechat.sh"
WECHAT_LOG_FILE="$RUNTIME_DIR/wechat/wechat_bot.log"
WECHAT_ACCOUNT_FILE="$RUNTIME_DIR/wechat/account.json"

# Shared env
DEFAULT_CWD="${DEFAULT_CWD:-$SCRIPT_DIR}"
CODEX_BIN="${CODEX_BIN:-}"
CODEX_SESSION_ROOT="${CODEX_SESSION_ROOT:-$HOME/.codex/sessions}"
CODEX_SANDBOX_MODE="${CODEX_SANDBOX_MODE:-}"
CODEX_APPROVAL_POLICY="${CODEX_APPROVAL_POLICY:-}"
CODEX_DANGEROUS_BYPASS="${CODEX_DANGEROUS_BYPASS:-0}"

# Telegram env
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
ALLOWED_TELEGRAM_USER_IDS="${ALLOWED_TELEGRAM_USER_IDS:-}"
TG_REQUIRE_ALLOWLIST="${TG_REQUIRE_ALLOWLIST:-1}"
TELEGRAM_INSECURE_SKIP_VERIFY="${TELEGRAM_INSECURE_SKIP_VERIFY:-0}"
TELEGRAM_CA_BUNDLE="${TELEGRAM_CA_BUNDLE:-}"
TG_STREAM_ENABLED="${TG_STREAM_ENABLED:-1}"
TG_STREAM_EDIT_INTERVAL_MS="${TG_STREAM_EDIT_INTERVAL_MS:-300}"
TG_STREAM_MIN_DELTA_CHARS="${TG_STREAM_MIN_DELTA_CHARS:-8}"
TG_THINKING_STATUS_INTERVAL_MS="${TG_THINKING_STATUS_INTERVAL_MS:-700}"
TG_VOICE_TRANSCRIBE_ENABLED="${TG_VOICE_TRANSCRIBE_ENABLED:-}"
TG_VOICE_TRANSCRIBE_BACKEND="${TG_VOICE_TRANSCRIBE_BACKEND:-local-whisper}"
TG_VOICE_TRANSCRIBE_MODEL="${TG_VOICE_TRANSCRIBE_MODEL:-gpt-4o-mini-transcribe}"
TG_VOICE_TRANSCRIBE_TIMEOUT_SEC="${TG_VOICE_TRANSCRIBE_TIMEOUT_SEC:-180}"
TG_VOICE_MAX_BYTES="${TG_VOICE_MAX_BYTES:-26214400}"
TG_VOICE_LOCAL_MODEL="${TG_VOICE_LOCAL_MODEL:-base}"
TG_VOICE_LOCAL_DEVICE="${TG_VOICE_LOCAL_DEVICE:-cpu}"
TG_VOICE_LOCAL_LANGUAGE="${TG_VOICE_LOCAL_LANGUAGE:-}"
TG_VOICE_FFMPEG_BIN="${TG_VOICE_FFMPEG_BIN:-}"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-}"

# Feishu env
FEISHU_APP_ID="${FEISHU_APP_ID:-}"
FEISHU_APP_SECRET="${FEISHU_APP_SECRET:-}"

# WeChat env
WECHAT_ENABLED="${WECHAT_ENABLED:-}"
WECHAT_API_BASE_URL="${WECHAT_API_BASE_URL:-https://ilinkai.weixin.qq.com}"
WECHAT_LOGIN_BOT_TYPE="${WECHAT_LOGIN_BOT_TYPE:-3}"
ALLOWED_WECHAT_USER_IDS="${ALLOWED_WECHAT_USER_IDS:-}"
WECHAT_REQUIRE_ALLOWLIST="${WECHAT_REQUIRE_ALLOWLIST:-1}"
WECHAT_POLL_TIMEOUT_SEC="${WECHAT_POLL_TIMEOUT_SEC:-35}"
WECHAT_SEND_TYPING="${WECHAT_SEND_TYPING:-1}"

resolve_codex_bin() {
  if [[ -n "$CODEX_BIN" ]]; then
    return 0
  fi
  if command -v codex >/dev/null 2>&1; then
    CODEX_BIN="$(command -v codex)"
  fi
}

has_tg_config() {
  [[ -n "$TELEGRAM_BOT_TOKEN" ]]
}

has_feishu_config() {
  [[ -n "$FEISHU_APP_ID" || -n "$FEISHU_APP_SECRET" ]]
}

has_wechat_enabled() {
  local raw="${WECHAT_ENABLED:-}"
  local lowered=""
  if [[ -z "$raw" ]]; then
    wechat_has_login
    return $?
  fi
  lowered="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"
  case "$lowered" in
    0|false|no|off|disable|disabled) return 1 ;;
    *) return 0 ;;
  esac
}

wechat_has_login() {
  [[ -f "$WECHAT_ACCOUNT_FILE" ]] && grep -q '"token"' "$WECHAT_ACCOUNT_FILE" 2>/dev/null
}

validate_tg_config() {
  if ! has_tg_config; then
    return 0
  fi
  if [[ ! "$TELEGRAM_BOT_TOKEN" =~ ^[0-9]{6,}:[A-Za-z0-9_-]{20,}$ ]]; then
    echo "[error] TELEGRAM_BOT_TOKEN 格式不正确（示例: 123456789:ABCDEF...）"
    exit 1
  fi
  if [[ -n "$ALLOWED_TELEGRAM_USER_IDS" ]] && [[ ! "$ALLOWED_TELEGRAM_USER_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
    echo "[error] ALLOWED_TELEGRAM_USER_IDS 格式应为数字（多个用户用逗号分隔）"
    exit 1
  fi
  if [[ "$TG_REQUIRE_ALLOWLIST" != "0" && -z "$ALLOWED_TELEGRAM_USER_IDS" ]]; then
    echo "[error] 出于安全默认要求配置 ALLOWED_TELEGRAM_USER_IDS"
    echo "[hint] 你可以设置你自己的 Telegram 数字 ID，例如: export ALLOWED_TELEGRAM_USER_IDS=\"123456789\""
    echo "[hint] 如果只是临时测试，可显式关闭: export TG_REQUIRE_ALLOWLIST=0"
    exit 1
  fi
  if [[ "$TELEGRAM_INSECURE_SKIP_VERIFY" == "1" ]]; then
    echo "[warn] TELEGRAM_INSECURE_SKIP_VERIFY=1 会关闭 Telegram TLS 证书校验（仅调试时使用）"
  fi
}

validate_feishu_config() {
  if ! has_feishu_config; then
    return 0
  fi
  if [[ -z "$FEISHU_APP_ID" || -z "$FEISHU_APP_SECRET" ]]; then
    echo "[error] FEISHU_APP_ID 和 FEISHU_APP_SECRET 必须同时设置"
    exit 1
  fi
}

validate_shared_config() {
  resolve_codex_bin
  if [[ ! -x "$CODEX_BIN" ]]; then
    echo "[error] CODEX_BIN 不存在或不可执行: $CODEX_BIN"
    exit 1
  fi
  if [[ ! -d "$DEFAULT_CWD" ]]; then
    echo "[error] DEFAULT_CWD 不存在或不是目录: $DEFAULT_CWD"
    exit 1
  fi
  if [[ "$DEFAULT_CWD" == "$HOME" || "$DEFAULT_CWD" == "/" ]]; then
    echo "[warn] DEFAULT_CWD 指向了过大的目录: $DEFAULT_CWD"
    echo "[warn] 建议改成更具体的项目目录，避免 bot 默认读写整个用户目录"
  fi
}

probe_tg_local_voice_env() {
  "$PYTHON_BIN" - <<'PY'
import importlib.util
import shutil

has_whisper = importlib.util.find_spec("whisper") is not None
has_ffmpeg = bool(shutil.which("ffmpeg"))
if not has_ffmpeg:
    try:
        import imageio_ffmpeg
        imageio_ffmpeg.get_ffmpeg_exe()
        has_ffmpeg = True
    except Exception:
        has_ffmpeg = False
print(f"{int(has_whisper)} {int(has_ffmpeg)}")
PY
}

configure_tg_voice_defaults() {
  if ! has_tg_config; then
    return 0
  fi

  local probe_result has_whisper has_ffmpeg
  probe_result="$(probe_tg_local_voice_env)"
  read -r has_whisper has_ffmpeg <<<"$probe_result"

  if [[ -z "$TG_VOICE_TRANSCRIBE_ENABLED" ]]; then
    if [[ "$has_whisper" == "1" && "$has_ffmpeg" == "1" ]]; then
      TG_VOICE_TRANSCRIBE_ENABLED="1"
      TG_VOICE_TRANSCRIBE_BACKEND="local-whisper"
      echo "[info] 检测到本地 Whisper 环境，自动启用 Telegram 本地语音转写"
    else
      TG_VOICE_TRANSCRIBE_ENABLED="0"
      echo "[warn] 未检测到可用的本地语音转写环境，Telegram 语音转写默认不启用"
      if [[ "$has_whisper" != "1" ]]; then
        echo "[warn] 缺少 whisper Python 包，可安装: python3 -m pip install --user -U openai-whisper torch"
      fi
      if [[ "$has_ffmpeg" != "1" ]]; then
        echo "[warn] 缺少 ffmpeg，可安装: brew install ffmpeg"
      fi
    fi
    return 0
  fi

  if [[ "$TG_VOICE_TRANSCRIBE_ENABLED" == "1" && "$TG_VOICE_TRANSCRIBE_BACKEND" == "local-whisper" ]]; then
    if [[ "$has_whisper" != "1" || "$has_ffmpeg" != "1" ]]; then
      echo "[warn] 已启用本地语音转写，但环境不完整；bot 启动后会在日志里提示原因"
      if [[ "$has_whisper" != "1" ]]; then
        echo "[warn] 可安装 whisper: python3 -m pip install --user -U openai-whisper torch"
      fi
      if [[ "$has_ffmpeg" != "1" ]]; then
        echo "[warn] 可安装 ffmpeg: brew install ffmpeg"
      fi
    fi
  fi
}

tg_is_running() {
  if [[ -f "$TG_PID_FILE" ]]; then
    local pid
    pid="$(cat "$TG_PID_FILE" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      return 0
    fi
    rm -f "$TG_PID_FILE"
  fi
  local existing_pid
  existing_pid="$(pgrep -f "$TG_BOT_SCRIPT" 2>/dev/null | head -n 1 || true)"
  if [[ -n "${existing_pid}" ]]; then
    echo "$existing_pid" >"$TG_PID_FILE"
    return 0
  fi
  return 1
}

tg_start() {
  mkdir -p "$RUNTIME_DIR"

  if tg_is_running; then
    echo "[info] Telegram 已在运行，PID=$(cat "$TG_PID_FILE")"
    return 0
  fi

  echo "[info] 启动 Telegram 服务..."
  nohup env \
    TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
    ALLOWED_TELEGRAM_USER_IDS="$ALLOWED_TELEGRAM_USER_IDS" \
    TG_REQUIRE_ALLOWLIST="$TG_REQUIRE_ALLOWLIST" \
    DEFAULT_CWD="$DEFAULT_CWD" \
    CODEX_BIN="$CODEX_BIN" \
    CODEX_SESSION_ROOT="$CODEX_SESSION_ROOT" \
    CODEX_SANDBOX_MODE="$CODEX_SANDBOX_MODE" \
    CODEX_APPROVAL_POLICY="$CODEX_APPROVAL_POLICY" \
    CODEX_DANGEROUS_BYPASS="$CODEX_DANGEROUS_BYPASS" \
    STATE_PATH="$TG_STATE_PATH" \
    TELEGRAM_INSECURE_SKIP_VERIFY="$TELEGRAM_INSECURE_SKIP_VERIFY" \
    TELEGRAM_CA_BUNDLE="$TELEGRAM_CA_BUNDLE" \
    TG_STREAM_ENABLED="$TG_STREAM_ENABLED" \
    TG_STREAM_EDIT_INTERVAL_MS="$TG_STREAM_EDIT_INTERVAL_MS" \
    TG_STREAM_MIN_DELTA_CHARS="$TG_STREAM_MIN_DELTA_CHARS" \
    TG_THINKING_STATUS_INTERVAL_MS="$TG_THINKING_STATUS_INTERVAL_MS" \
    TG_VOICE_TRANSCRIBE_ENABLED="$TG_VOICE_TRANSCRIBE_ENABLED" \
    TG_VOICE_TRANSCRIBE_BACKEND="$TG_VOICE_TRANSCRIBE_BACKEND" \
    TG_VOICE_TRANSCRIBE_MODEL="$TG_VOICE_TRANSCRIBE_MODEL" \
    TG_VOICE_TRANSCRIBE_TIMEOUT_SEC="$TG_VOICE_TRANSCRIBE_TIMEOUT_SEC" \
    TG_VOICE_MAX_BYTES="$TG_VOICE_MAX_BYTES" \
    TG_VOICE_LOCAL_MODEL="$TG_VOICE_LOCAL_MODEL" \
    TG_VOICE_LOCAL_DEVICE="$TG_VOICE_LOCAL_DEVICE" \
    TG_VOICE_LOCAL_LANGUAGE="$TG_VOICE_LOCAL_LANGUAGE" \
    TG_VOICE_FFMPEG_BIN="$TG_VOICE_FFMPEG_BIN" \
    OPENAI_API_KEY="$OPENAI_API_KEY" \
    OPENAI_BASE_URL="$OPENAI_BASE_URL" \
    "$PYTHON_BIN" -u "$TG_BOT_SCRIPT" >>"$TG_LOG_FILE" 2>&1 &

  local pid=$!
  echo "$pid" >"$TG_PID_FILE"
  sleep 1

  if kill -0 "$pid" >/dev/null 2>&1; then
    echo "[ok] Telegram 已启动，PID=$pid"
    echo "[ok] Telegram 日志: $TG_LOG_FILE"
  else
    rm -f "$TG_PID_FILE"
    echo "[error] Telegram 启动失败，最近日志如下:"
    tail -n 50 "$TG_LOG_FILE" || true
    exit 1
  fi
}

tg_stop() {
  if tg_is_running; then
    local pid
    pid="$(cat "$TG_PID_FILE")"
    kill "$pid" >/dev/null 2>&1 || true
    rm -f "$TG_PID_FILE"
    echo "[ok] Telegram 已停止，PID=$pid"
  else
    echo "[info] Telegram 未运行"
  fi
}

tg_status() {
  if tg_is_running; then
    echo "[ok] Telegram 运行中，PID=$(cat "$TG_PID_FILE")"
  else
    echo "[info] Telegram 未运行"
  fi
}

feishu_start() {
  if [[ ! -x "$FEISHU_RUN_SCRIPT" ]]; then
    echo "[error] 找不到飞书启动脚本: $FEISHU_RUN_SCRIPT"
    exit 1
  fi
  echo "[info] 启动飞书服务..."
  "$FEISHU_RUN_SCRIPT" start
}

feishu_stop() {
  if [[ -x "$FEISHU_RUN_SCRIPT" ]]; then
    "$FEISHU_RUN_SCRIPT" stop
  else
    echo "[info] 飞书脚本不存在，跳过停止"
  fi
}

feishu_status() {
  if [[ -x "$FEISHU_RUN_SCRIPT" ]]; then
    "$FEISHU_RUN_SCRIPT" status
  else
    echo "[info] 飞书脚本不存在"
  fi
}

wechat_start() {
  if [[ ! -x "$WECHAT_RUN_SCRIPT" ]]; then
    echo "[error] 找不到微信启动脚本: $WECHAT_RUN_SCRIPT"
    exit 1
  fi
  echo "[info] 启动微信服务..."
  "$WECHAT_RUN_SCRIPT" start
}

wechat_stop() {
  if [[ -x "$WECHAT_RUN_SCRIPT" ]]; then
    "$WECHAT_RUN_SCRIPT" stop
  else
    echo "[info] 微信脚本不存在，跳过停止"
  fi
}

wechat_status() {
  if [[ -x "$WECHAT_RUN_SCRIPT" ]]; then
    "$WECHAT_RUN_SCRIPT" status
  else
    echo "[info] 微信脚本不存在"
  fi
}

start() {
  validate_tg_config
  validate_feishu_config
  validate_shared_config
  configure_tg_voice_defaults

  if [[ "${CODEX_DANGEROUS_BYPASS}" == "0" ]]; then
    echo "[info] 当前 CODEX_DANGEROUS_BYPASS=0，没有提升权限参数。"
    echo "[info] 如需启用危险权限模式，可显式设置: export CODEX_DANGEROUS_BYPASS=1"
  fi

  local can_start_wechat=0
  if has_wechat_enabled && wechat_has_login; then
    can_start_wechat=1
  fi

  if ! has_tg_config && ! has_feishu_config && [[ "$can_start_wechat" != "1" ]]; then
    echo "[error] 未检测到可启动的渠道配置。"
    echo "请至少配置一项："
    echo "  1) TELEGRAM_BOT_TOKEN"
    echo "  2) FEISHU_APP_ID + FEISHU_APP_SECRET"
    echo "  3) 已执行 ./run_wechat.sh login（且未显式关闭微信）"
    exit 1
  fi

  if has_tg_config; then
    tg_start
  else
    echo "[info] 未配置 TELEGRAM_BOT_TOKEN，跳过 Telegram"
  fi

  if has_feishu_config; then
    feishu_start
  else
    echo "[info] 未配置 FEISHU_APP_ID/FEISHU_APP_SECRET，跳过飞书"
  fi

  if has_wechat_enabled; then
    if wechat_has_login; then
      wechat_start
    else
      echo "[warn] 已启用微信渠道，但尚未完成登录。"
      echo "[hint] 先执行: ./run_wechat.sh login"
    fi
  else
    echo "[info] 微信已显式关闭，跳过微信"
  fi
}

stop() {
  tg_stop
  feishu_stop
  wechat_stop
}

status() {
  tg_status
  feishu_status
  wechat_status
}

logs() {
  mkdir -p "$RUNTIME_DIR"
  mkdir -p "$RUNTIME_DIR/wechat"
  touch "$TG_LOG_FILE" "$FEISHU_LOG_FILE" "$WECHAT_LOG_FILE"
  tail -f "$TG_LOG_FILE" "$FEISHU_LOG_FILE" "$WECHAT_LOG_FILE"
}

restart() {
  stop
  start
}

usage() {
  cat <<EOF
用法: ./run.sh [start|stop|restart|status|logs]
默认: start

说明：
- 配置 TELEGRAM_BOT_TOKEN -> 启动 Telegram
- 配置 FEISHU_APP_ID + FEISHU_APP_SECRET -> 启动飞书
- 已登录微信 -> 默认启动微信
- 设置 WECHAT_ENABLED=0 -> 显式关闭微信
- 可以组合启动多个渠道

示例：
export TELEGRAM_BOT_TOKEN=\"123456:xxxx\"
export ALLOWED_TELEGRAM_USER_IDS=\"123456789\"   # 建议设置白名单
export TG_STREAM_ENABLED=1                      # 可选：1=启用流式编辑回复，0=关闭
export TG_STREAM_EDIT_INTERVAL_MS=300           # 可选：流式编辑刷新节流间隔（毫秒）
export TG_STREAM_MIN_DELTA_CHARS=8              # 可选：增量字符数太小时跳过刷新
export TG_THINKING_STATUS_INTERVAL_MS=700       # 可选：思考状态刷新间隔（毫秒）

export FEISHU_APP_ID=\"cli_xxx\"
export FEISHU_APP_SECRET=\"xxx\"

export WECHAT_ENABLED=0           # 可选：已登录微信时默认启用；设为 0 可关闭
export ALLOWED_WECHAT_USER_IDS=\"xxx@im.wechat\"
export WECHAT_REQUIRE_ALLOWLIST=1
export WECHAT_API_BASE_URL=\"https://ilinkai.weixin.qq.com\"
export WECHAT_LOGIN_BOT_TYPE=3
export WECHAT_POLL_TIMEOUT_SEC=35
export WECHAT_SEND_TYPING=1
# 首次使用微信前先执行 ./run_wechat.sh login

# Codex command execution policy
# 0: no extra permission args (default)
# 1: defaults to sandbox_mode=danger-full-access + approval_policy=never
# 2: append --dangerously-bypass-approvals-and-sandbox
export CODEX_SANDBOX_MODE=\"\"   # optional override for level=1
export CODEX_APPROVAL_POLICY=\"\" # optional override for level=1
export CODEX_DANGEROUS_BYPASS=0
EOF
}

cmd="${1:-start}"
case "$cmd" in
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
