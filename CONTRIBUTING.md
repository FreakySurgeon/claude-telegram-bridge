# Contributing to Claude Telegram Bridge

Thanks for your interest in contributing!

## Getting Started

1. Fork the repo
2. Clone your fork: `git clone https://github.com/your-username/claude-telegram-bridge.git`
3. Install dependencies: `uv sync`
4. Create a branch: `git checkout -b feat/my-feature`

## Development

```bash
# Run locally
uv run uvicorn claude_telegram.main:app --reload

# Run tests
uv run pytest -v

# Run with coverage
uv run pytest --cov=claude_telegram
```

## Pull Requests

- Keep PRs focused on a single change
- Add tests for new features
- Follow existing code style
- Update README if adding user-facing features

## Reporting Issues

Open an issue with:
- Steps to reproduce
- Expected behavior
- Actual behavior
- Python version and OS
