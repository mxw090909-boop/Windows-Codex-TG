# tg-codex

Language: English | [简体中文](README.zh-CN.md)

`tg-codex` lets you run and continue local `codex` sessions from chat apps. It supports Telegram, Feishu (long connection), and WeChat.

## Features

- List local session history with titles
- Switch to an existing session and continue asking
- Keep receiving commands while a session is running, and switch to another thread
- Create new sessions and control working directory
- View recent messages in a session (`/history`)
- Optionally transcribe Telegram voice/audio messages into text before continuing the session
- Download Telegram photo/document messages into the current workspace before continuing the session
- Keep a lightweight per-user memory store, let the bot write back important facts automatically, and manage it from chat
- Run Telegram only, Feishu only, WeChat only, or any combination at the same time

## Requirements

- Python 3.9+
- Local `codex` installed and already logged in
- Channel credentials (as needed)
  - Telegram: `TELEGRAM_BOT_TOKEN`
  - Feishu: `FEISHU_APP_ID` + `FEISHU_APP_SECRET`
  - WeChat: run `./run_wechat.sh login` once to create a local bot token

## Quick Start

### 1) Configure environment variables (as needed)

```bash
# Telegram (optional)
export TELEGRAM_BOT_TOKEN="your bot token"
export ALLOWED_TELEGRAM_USER_IDS="123456789"         # recommended; required by default for safety
export TG_REQUIRE_ALLOWLIST=1                         # optional, default 1; set 0 to allow any Telegram user
export TELEGRAM_INSECURE_SKIP_VERIFY=0                # optional, default 0; set 1 only for temporary debugging
export TG_STREAM_ENABLED=1                            # optional, default 1 (streaming reply edits)
export TG_STREAM_EDIT_INTERVAL_MS=300                # optional, stream edit throttle interval in ms
export TG_STREAM_MIN_DELTA_CHARS=8                    # optional, skip refresh if change is too small
export TG_THINKING_STATUS_INTERVAL_MS=700             # optional, thinking status refresh interval in ms
export TG_MEMORY_PATH="./bot_memory.json"             # optional, Telegram memory store path
export TG_MEMORY_AUTO_ENABLED=1                       # optional, default 1; auto writeback only runs when a private writeback prompt is configured
export TG_USER_DISPLAY_NAME="friend"                  # optional, default "对方" in local prompt wrappers
export TG_VOICE_TRANSCRIBE_ENABLED=1                  # optional; if unset, run.sh auto-enables when local env is ready
export TG_VOICE_TRANSCRIBE_BACKEND="local-whisper"    # optional, default local-whisper
export TG_VOICE_MAX_BYTES=26214400                    # optional, max Telegram audio bytes to transcribe

# Local Whisper backend (no external API)
export TG_VOICE_LOCAL_MODEL="base"                    # optional
export TG_VOICE_LOCAL_DEVICE="cpu"                    # optional: cpu | cuda | mps
export TG_VOICE_LOCAL_LANGUAGE="zh"                   # optional
export TG_VOICE_FFMPEG_BIN="/opt/homebrew/bin/ffmpeg" # optional, auto-detected if omitted

# OpenAI backend (optional fallback)
export OPENAI_API_KEY="sk-..."                        # required only when backend=openai
export OPENAI_BASE_URL="https://api.openai.com/v1"    # optional
export TG_VOICE_TRANSCRIBE_MODEL="gpt-4o-mini-transcribe"  # optional for backend=openai
export TG_VOICE_TRANSCRIBE_TIMEOUT_SEC=180            # optional

# Feishu (optional)
export FEISHU_APP_ID="cli_xxx"
export FEISHU_APP_SECRET="xxx"

# WeChat (optional)
export WECHAT_ENABLED=0                            # optional; logged-in WeChat is enabled by default, set 0 to disable
export ALLOWED_WECHAT_USER_IDS="xxx@im.wechat"    # optional; defaults to the scanner's own user_id after login
export WECHAT_REQUIRE_ALLOWLIST=1                  # optional, default 1; set 0 to allow any WeChat user
export WECHAT_API_BASE_URL="https://ilinkai.weixin.qq.com"
export WECHAT_LOGIN_BOT_TYPE=3
export WECHAT_POLL_TIMEOUT_SEC=35
export WECHAT_SEND_TYPING=1

# Shared (optional)
export DEFAULT_CWD="/path/to/your/project/codex-tg"
export CODEX_BIN="/Applications/Codex.app/Contents/Resources/codex"
export CODEX_SESSION_ROOT="$HOME/.codex/sessions"
export CODEX_SANDBOX_MODE=""                         # optional: used only when CODEX_DANGEROUS_BYPASS=1
export CODEX_APPROVAL_POLICY=""                      # optional: used only when CODEX_DANGEROUS_BYPASS=1
export CODEX_DANGEROUS_BYPASS=0                      # 0/1/2 (see permission section below)
export CODEX_IDLE_TIMEOUT_SEC=3600                  # optional: kill codex exec after this many idle seconds with no output; 0 disables it
export TG_NEW_THREAD_PERSONA_PROMPT_PATH="./.local-prompts/new-thread-persona.txt"
export TG_HEARTBEAT_SESSION_PROMPT_PATH="./.local-prompts/heartbeat-session.txt"
export TG_HEARTBEAT_BANNED_PATTERNS_PATH="./.local-prompts/heartbeat-banned-patterns.txt"
export TG_HEARTBEAT_TEMPLATE_MESSAGES_PATH="./.local-prompts/heartbeat-template-messages.txt"
export TG_HEARTBEAT_FOLLOWUP_TEMPLATE_MESSAGES_PATH="./.local-prompts/heartbeat-followup-template-messages.txt"
export TG_MEMORY_CONTEXT_PROMPT_PATH="./.local-prompts/memory-context.txt"
export TG_MEMORY_WRITEBACK_PROMPT_PATH="./.local-prompts/memory-writeback.txt"
```

### 2) Start services

```bash
./run.sh start
```

`run.sh` startup behavior:

- `TELEGRAM_BOT_TOKEN` configured: starts Telegram
- `FEISHU_APP_ID` + `FEISHU_APP_SECRET` configured: starts Feishu
- a saved WeChat login token: starts WeChat by default
- `WECHAT_ENABLED=0`: disables WeChat explicitly
- any combination configured: starts all available channels

Security notes:

- `run.sh` now refuses to start Telegram unless `ALLOWED_TELEGRAM_USER_IDS` is set, unless you explicitly set `TG_REQUIRE_ALLOWLIST=0`
- `TELEGRAM_INSECURE_SKIP_VERIFY` defaults to `0`; keep it there unless you are debugging a local TLS issue
- if WeChat login has not been completed yet, `run.sh` prints a clear hint to run `./run_wechat.sh login`

Common commands:

```bash
./run.sh stop
./run.sh status
./run.sh logs
./run.sh restart
```

## WeChat Login

The WeChat channel uses Tencent's new QR-login flow and stores the resulting token under `.runtime/wechat/`.

First-time setup:

```bash
./run_wechat.sh login
./run_wechat.sh start
```

WeChat-only management:

```bash
./run_wechat.sh login
./run_wechat.sh stop
./run_wechat.sh status
./run_wechat.sh logs
./run_wechat.sh restart
```

Notes:

- One logged-in WeChat account is supported per service instance in this version
- With allowlist protection enabled, the scanner's own `user_id` is auto-allowed by default after login
- v1 is text-only: no images, files, voice, groups, or edit-style streaming replies yet
- WeChat uses the same session commands as Telegram and Feishu

## Feishu Setup

Feishu uses official SDK long connection mode (no public callback URL required).

### Feishu app requirements

- Enable Bot capability
- Subscribe to event: `im.message.receive_v1`
- Publish and install the app in your tenant

### Optional Feishu env vars

```bash
export ALLOWED_FEISHU_OPEN_IDS="ou_xxx,ou_yyy"   # optional open_id allowlist
export FEISHU_ENABLE_P2P=1                         # default 1 (DM enabled), set 0 for group-only
export FEISHU_LOG_LEVEL="INFO"                  # DEBUG/INFO/WARN/ERROR
export FEISHU_RICH_MESSAGE=1                       # default 1, render replies as rich cards
```

Notes:

- With `FEISHU_RICH_MESSAGE=1`, replies are sent as card markdown (titles/lists/code blocks)
- To manage Feishu only, use `./run_feishu.sh start|stop|status|logs|restart`

## Permission Switches & Risks

Permission behavior is controlled by `CODEX_DANGEROUS_BYPASS`:

- `0` (default): no extra permission flags (least privilege)
- `1`: enable permission flags
  - `CODEX_SANDBOX_MODE` defaults to `danger-full-access` (override allowed)
  - `CODEX_APPROVAL_POLICY` defaults to `never` (override allowed)
- `2`: append `--dangerously-bypass-approvals-and-sandbox`

Notes:
- `CODEX_SANDBOX_MODE` / `CODEX_APPROVAL_POLICY` are applied only when `CODEX_DANGEROUS_BYPASS=1`
- `CODEX_DANGEROUS_BYPASS=2` takes full bypass path

Risk notes:

- It may execute arbitrary commands and modify/delete local files
- It may read and exfiltrate sensitive data (keys, configs, source code)
- Enable only in controlled environments and switch back to `0` afterward

## Commands (Telegram / Feishu / WeChat)

- `/help`
- `/sessions [N]`: list recent `N` sessions (title + index)
- `/use <index|session_id>`: switch active session
- `/history [index|session_id] [N]`: show latest `N` messages (default 10, max 50)
- `/new [cwd]`: enter new-session mode; next normal message creates a new session
- `/status`: show current active session
- `/ask <text>`: ask in the current session
- `/memory [list]`: show recent memories
- `/memory add <text>`: save a memory manually
- `/memory forget <id>`: delete a memory
- `/memory pin <id>` / `/memory unpin <id>`: keep a memory at the top or remove the pin
- `/memory search <keyword>`: search saved memories

## Telegram Voice Messages

Telegram voice and audio messages can be transcribed and then sent into the current Codex session as text.

Notes:

- `local-whisper` does not call an external API; it uses local `whisper` plus `ffmpeg`
- `run.sh` and `run.ps1` now probe the local environment on startup: if local Whisper is ready, they auto-enable Telegram voice transcription by default
- If local dependencies are missing, `run.sh` prints install commands and leaves voice transcription disabled by default
- This is currently Telegram-only; Feishu still handles text messages only
- Captions on Telegram audio messages are appended as extra context before the transcript
- If transcription is not configured, the bot will reply with a clear hint instead of silently ignoring the message
- Send normal text directly: continue current session, or create one if in new-session mode

## Telegram Photos and Documents

Telegram photo and document messages are downloaded into the current session workspace under `.codex-tg-attachments/` before Codex continues the conversation.

Notes:

- Photo messages are attached to the initial Codex prompt as images
- Image documents are also attached as images, and all files are saved locally so Codex can inspect them by path
- Captions are included as extra context alongside the downloaded file

## Telegram TTS Replies

Telegram can also append a local GPT-SoVITS voice note after a normal text reply.

Notes:

- Text replies still go out as normal; TTS adds a separate voice note after the text
- `TG_TTS_MODE=auto` only speaks short conversational replies and skips code blocks, file paths, command output, and long responses
- The bot auto-starts `api_v2.py` from `TG_TTS_GSV_ROOT` on first use when the local GPT-SoVITS environment is ready
- Typical local config looks like this:

```bash
export TG_TTS_ENABLED=1
export TG_TTS_BACKEND="local-gpt-sovits"
export TG_TTS_MODE="auto"
export TG_TTS_API_BASE="http://127.0.0.1:9880"
export TG_TTS_GSV_ROOT="C:/COVE/GPT-SoVITS/GPT-SoVITS"
export TG_TTS_REF_AUDIO_PATH="C:/COVE/Cove_GSV/Cove_GSV/reference_audios/中文/emotions/【默认】还有编写和调试计算机程序的能力。.wav"
export TG_TTS_PROMPT_TEXT="还有编写和调试计算机程序的能力。"
export TG_TTS_GPT_WEIGHTS="GPT_weights/Cove_ZH-e12.ckpt"
export TG_TTS_SOVITS_WEIGHTS="SoVITS_weights/Cove_ZH_e8_s320.pth"
```

## Telegram Memory

Telegram now keeps a small per-user memory store in a separate JSON file instead of mixing it into the runtime state.

Notes:

- The bot injects pinned memories and the most relevant matching memories into prompts when a new chat starts or the current topic matches
- After a successful reply, the bot can run a small background Codex pass to extract only durable facts worth remembering
- `/memory` gives you a manual escape hatch so you can review, pin, delete, or add memories yourself
- The default file path is `./bot_memory.json`, and you can override it with `TG_MEMORY_PATH` or `MEMORY_PATH`
- The public repo ships blank memory prompt defaults; automatic prompt injection and writeback stay idle until you provide local override files

## Private Prompt Overrides

The repository now ships blank defaults for new-thread persona, heartbeat, and memory prompt injection/writeback.

If you have private persona text, custom heartbeat style, memory prompts, or project-specific prompt instructions that should not be published, keep them in local files such as `.local-prompts/` and point the environment variables above at those files. The repo `.gitignore` already excludes that directory.

Tips:

- After `/sessions`, send an index directly (for example `1`) to switch
- During long-running tasks, you can still send `/use`, `/sessions`, and `/status`
- In Feishu group chats, it is recommended to `@bot` before sending commands

## Additional Scripts

- `tg_codex_bot.py`: Telegram service entry
- `feishu_longconn_service.py`: Feishu long-connection service entry
- `run_feishu.sh`: Feishu-only process management script
- `wechat_codex_service.py`: WeChat service entry
- `run_wechat.sh`: WeChat-only process management script
- `codex_common.py`: shared local Codex/session/state helpers

## Known Limitations

- New sessions are mainly visible in terminal/CLI session history
- Codex Desktop may need restart before newly continued sessions become visible
- Only one in-flight task is allowed per session; switch to another thread for parallel work
- WeChat currently supports direct-message text only
