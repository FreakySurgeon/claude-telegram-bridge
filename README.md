# Claude Telegram Bridge

Control [Claude Code](https://docs.anthropic.com/en/docs/claude-code) remotely via Telegram. A Python/FastAPI bridge that lets you interact with Claude Code from anywhere using your phone.

## Features

- **Multi-session support** -- Run Claude in different project directories simultaneously
- **Permission handling** -- Approve or deny Claude's permission requests via inline buttons
- **Forum Topics** -- Each conversation auto-creates a Telegram forum topic with a descriptive title
- **Voice messages** -- Send voice notes; transcribed locally with Whisper or via Voxtral API
- **Photo analysis** -- Send images directly to Claude for vision-based analysis
- **Animated status** -- Rotating "Thinking...", "Pondering..." etc. while Claude works
- **Auto-continue** -- Just reply naturally, no commands needed (10-minute window)
- **Quick-reply buttons** -- Tap numbered options directly
- **Markdown rendering** -- Claude's markdown converted to Telegram HTML
- **Hook notifications** -- Get notified in Telegram when local Claude finishes a task
- **Three connection modes** -- Polling (default), Tunnel, or Webhook

## Quick Start

### 1. Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. Choose a name (e.g., "My Claude Bot")
4. Choose a username (must end in `bot`, e.g., `my_claude_bot`)
5. **Save the bot token** -- looks like `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`

### 2. Get Your Chat ID

1. Start a chat with your new bot (search for it by username)
2. Send any message to it (e.g., "hello")
3. Open this URL in your browser (replace `YOUR_BOT_TOKEN`):
   ```
   https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates
   ```
4. Find `"chat":{"id":` in the response -- that number is your chat ID

### 3. Set Up a Telegram Group with Topics

The bot works best in a **group with forum topics enabled** — each conversation gets its own thread.

1. Create a new Telegram group (or use an existing one)
2. Go to **Group Settings > Topics** and enable **Topics**
3. Add your bot to the group and **make it admin** (it needs permission to create/edit topics)
4. Get the **group chat ID** (it will be negative, e.g., `-1001234567890`):
   - Send a message in the group
   - Check `https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates`
   - Use the `chat.id` from the group message
5. Use this group chat ID as your `TELEGRAM_CHAT_ID` in `.env`

> **Note:** The bot also works in direct messages (without topics), but you lose the threaded conversation history.

### 4. Install

```bash
# Clone the repo
git clone https://github.com/FreakySurgeon/claude-telegram-bridge.git
cd claude-telegram-bridge

# Install dependencies
uv sync
```

### 5. Configure

```bash
cp .env.example .env
```

Edit `.env`:
```env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_CHAT_ID=123456789
CLAUDE_CLI_PATH=claude
CLAUDE_WORKING_DIR=/path/to/your/project
MODE=polling
```

### 6. Run

```bash
uv run uvicorn claude_telegram.main:app --host 0.0.0.0 --port 8000
```

You should see:
```
Starting polling loop...
Polling started successfully
Application startup complete.
```

Now send a message to your bot!

## Connection Modes

### Polling Mode (Default)

No public URL needed -- polls Telegram's servers directly. Simple and reliable.

```bash
uv run uvicorn claude_telegram.main:app
```

### Tunnel Mode

Uses Cloudflare's free quick tunnels to create a public URL automatically. Lower latency but requires DNS propagation (2-5 minutes on first start).

```bash
# Install cloudflared first
# macOS: brew install cloudflared
# Linux: curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
#   -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared

MODE=tunnel uv run uvicorn claude_telegram.main:app
```

### Webhook Mode

Use your own public URL (e.g., behind nginx, Caddy, or a cloud provider).

```bash
MODE=webhook WEBHOOK_URL=https://your-domain.com uv run uvicorn claude_telegram.main:app
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start`, `/help` | Show help with formatted commands |
| `/c <message>` | Continue previous session |
| `/new <message>` | Start fresh session (reset context) |
| `/dir <path>` | Switch to a different directory/session |
| `/dirs` | List all active sessions |
| `/repos` | Quick-switch to a favorite repo |
| `/rmdir <path>` | Remove a session from the list |
| `/compact` | Compact conversation context |
| `/cancel` | Cancel current running task |
| `/status` | Check if Claude is running |
| `<any text>` | Auto-continues if within 10 min, else new session |

**Tips:**
- Just type naturally -- conversations auto-continue for 10 minutes
- Quick replies like "1", "2", "yes", "no" always continue the current session
- Tap inline buttons for numbered options

## Multi-Directory Sessions

Run Claude in different project directories simultaneously. Each directory maintains its own conversation context and history.

> **Important:** Avoid using `/dir` on directories where you're actively running Claude locally. The bot and local CLI share the same session files (`~/.claude/projects/`), which can cause conflicts. Use the bot for directories you're not working on locally, or close your local Claude session first.

**Add a new directory:**
```
/dir ~/projects/backend
```

**Switch between sessions:** Use `/dirs` to see all sessions with numbered buttons:
```
You: /dirs
Bot: Active Sessions
     > 1. frontend
       2. api
     [1. frontend] [2. api]   <-- tap to switch
```

**Example workflow:**
```
You: /dir ~/projects/api
Bot: Switched to api
     Status: idle

You: add input validation to the user endpoint
Bot: [api] Thinking...
Bot: I'll add validation to src/routes/user.ts...

You: /dir ~/projects/frontend
Bot: Switched to frontend

You: /dirs
Bot: Active Sessions
     > 1. frontend
       2. api
     [1. frontend] [2. api]

You: *taps [2. api] button*
Bot: Switched to api
     Status: idle, in conversation
```

Each session:
- **Resumes from stored Claude sessions** -- picks up where you left off via `~/.claude/projects/`
- **Shows previous context** -- displays your last messages when switching to a stored session
- Has its own 10-minute auto-continue window
- Maintains separate conversation context
- Shows directory name in status messages (e.g., `[api] Thinking...`)
- Quick-switch via numbered buttons

## Forum Topics

When used in a Telegram group with forum topics enabled, the bot automatically creates a new topic for each conversation. Topics are named with the date and a summary of the conversation:

```
[api] 15/02 - Add input validation to user endpoint
[frontend] 15/02 - Fix responsive layout on mobile
```

The topic title is generated from:
1. A `<!-- title: ... -->` comment in Claude's response (if Claude includes one)
2. A local LLM fallback (Ollama) for automatic summarization
3. A truncated version of your first message as a last resort

This keeps your Telegram group organized with a clear history of all conversations.

## Voice Messages

Send a voice message to the bot and it will be transcribed automatically:

- **Short messages (< 5 min):** Transcribed locally using [whisper.cpp](https://github.com/ggerganov/whisper.cpp) -- fast, private, no API calls
- **Long messages (>= 5 min):** Transcribed via [Mistral Voxtral API](https://docs.mistral.ai/) for better accuracy on longer audio

After transcription, the text is shown to you with a "Send to Claude" button for confirmation before processing.

**Setup (optional):**
```bash
# For local Whisper transcription
# 1. Build whisper.cpp: https://github.com/ggerganov/whisper.cpp
# 2. Download a model (e.g., medium)
# 3. Set in .env:
WHISPER_BIN=/opt/whisper.cpp/build/bin/whisper-cli
WHISPER_MODEL=/opt/whisper.cpp/models/ggml-medium.bin

# For Voxtral API (longer audio)
MISTRAL_API_KEY=your_mistral_key
```

## Photo Analysis

Send a photo to the bot and Claude will analyze it using vision capabilities. The image is downloaded and passed to Claude, which can describe contents, read text, analyze diagrams, review screenshots, and more.

## Permission Handling

When Claude needs to perform actions requiring permission (writing files, running commands, etc.), you get an interactive prompt:

```
You: create a file /tmp/hello.txt with hello world

Bot: Permission denied:
     Write to /tmp/hello.txt

     I need write permission for /tmp/hello.txt.

     [Allow & Retry] [Deny]
```

- **Allow & Retry** -- Grants permission and retries the action
- **Deny** -- Cancels the request

Supported permission types: `Write`, `Edit`, `Read`, `Bash`

## Hook Notifications

Get notified in Telegram when Claude finishes a task in your local terminal. The included `hook.py` script reads the session summary and sends it to the bot.

**Setup:**

Add to your `~/.claude/settings.json`:
```json
{
  "hooks": {
    "Stop": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "python /path/to/claude-telegram-bridge/hook.py completed",
        "timeout": 30000
      }]
    }]
  }
}
```

When Claude finishes locally, the hook:
1. Reads the latest session file from `~/.claude/projects/`
2. Extracts a summary of what Claude did (from the result or last assistant message)
3. Sends a notification to your Telegram bot with the summary

Set `HOOK_SERVER_URL` in `.env` if the bot runs on a different host (defaults to `http://localhost:8000`).

The hook automatically skips notifications when Claude was triggered by the Telegram bot itself (to avoid duplicates).

## Docker

```bash
# Build
docker build -t claude-telegram-bridge .

# Run with polling (default)
docker run -d \
  -e TELEGRAM_BOT_TOKEN=your_token \
  -e TELEGRAM_CHAT_ID=your_chat_id \
  -e MODE=polling \
  -v /usr/local/bin/claude:/usr/local/bin/claude:ro \
  -v $(pwd):/workspace \
  claude-telegram-bridge

# Or use docker-compose
docker compose up -d
```

## Systemd Service

```bash
# Copy and edit the service file
sudo cp claude-telegram.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now claude-telegram

# Check status
sudo systemctl status claude-telegram
sudo journalctl -u claude-telegram -f
```

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | (required) | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | (required) | Your chat ID (security: only this chat can use the bot) |
| `CLAUDE_CLI_PATH` | `claude` | Path to Claude CLI binary |
| `CLAUDE_WORKING_DIR` | (none) | Default working directory for Claude |
| `MODE` | `polling` | Connection mode: `polling`, `tunnel`, or `webhook` |
| `HOST` | `0.0.0.0` | Server bind host |
| `PORT` | `8000` | Server bind port |
| `WEBHOOK_URL` | (none) | Your public URL (webhook mode only) |
| `FAVORITE_REPOS` | (none) | Comma-separated paths relative to `~` for `/repos` |
| `MISTRAL_API_KEY` | (none) | Mistral API key for Voxtral transcription |
| `WHISPER_BIN` | `/opt/whisper.cpp/build/bin/whisper-cli` | Path to whisper.cpp binary |
| `WHISPER_MODEL` | `/opt/whisper.cpp/models/ggml-medium.bin` | Path to Whisper model |
| `HOOK_SERVER_URL` | `http://localhost:8000` | Bot server URL for hook notifications |

## Troubleshooting

### Bot doesn't respond

1. Check the chat ID matches your `.env`
2. Make sure you messaged the bot first (it cannot initiate conversations)
3. Check server logs for errors

### "Claude is busy"

Claude is still processing a previous request. Use `/cancel` to stop it, or wait for it to finish.

### "Webhook setup failed" / DNS errors (tunnel mode)

This is normal for tunnel mode. Cloudflare quick tunnels take 2-5 minutes for DNS to propagate. The app retries automatically with exponential backoff. Just wait.

## Development

```bash
# Run with auto-reload
uv run uvicorn claude_telegram.main:app --reload

# Run tests
uv run pytest -v

# Run with coverage
uv run pytest --cov=claude_telegram
```

## Project Structure

```
claude-telegram-bridge/
├── src/claude_telegram/
│   ├── main.py          # FastAPI app, polling/webhook handlers, commands
│   ├── config.py        # Pydantic settings from .env
│   ├── bots.py          # BotConfig dataclass
│   ├── claude.py        # Claude CLI runner, session management
│   ├── telegram.py      # Telegram API client
│   ├── topic.py         # Forum topic auto-naming
│   ├── transcribe.py    # Whisper + Voxtral transcription
│   ├── markdown.py      # Markdown to Telegram HTML
│   └── tunnel.py        # Cloudflare tunnel manager
├── tests/               # Pytest suite
├── hook.py              # Hook notification script
├── Dockerfile
├── docker-compose.yml
└── docker-entrypoint.sh
```

## Attribution

This project builds on the work of others:

- **[claude-code-telegram-bot](https://github.com/AmoMor/claude-code-telegram-bot)** by [Amit Mor](https://github.com/AmoMor) -- The original Telegram bot for Claude Code that inspired and provided the foundation for this project
- **[Claude Code Remote](https://github.com/anthropics/Claude-Code-Remote)** by [Anthropic](https://github.com/anthropics) -- The reference implementation for remote Claude Code interaction

## License

MIT -- see [LICENSE](LICENSE) for details.
