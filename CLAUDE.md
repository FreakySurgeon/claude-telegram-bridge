# Claude Telegram Bridge

Telegram bot that bridges Claude Code for remote development.

## Development

```bash
uv sync
uv run uvicorn claude_telegram.main:app --reload
uv run pytest -v
```

## Project Structure

```
src/claude_telegram/
├── main.py          # FastAPI app, message handlers, commands
├── config.py        # Pydantic settings from .env
├── bots.py          # BotConfig dataclass
├── claude.py        # ClaudeRunner, SessionManager
├── telegram.py      # Telegram API wrapper
├── topic.py         # Forum topic naming
├── transcribe.py    # Whisper + Voxtral transcription
├── markdown.py      # MD → Telegram HTML
└── tunnel.py        # Cloudflare tunnel
```

## Tests

```bash
uv run pytest -v
uv run pytest --cov=claude_telegram
```
