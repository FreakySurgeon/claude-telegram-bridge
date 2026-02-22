"""FastAPI application - Telegram webhook handler."""

import asyncio
import html
import logging
import random
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request

from .bots import BotConfig, create_bots
from .transcribe import transcribe_audio

# Claude Code spinner words (from the CLI)
# Source: https://github.com/levindixon/tengu_spinner_words
SPINNER_VERBS = [
    "Accomplishing", "Actioning", "Actualizing", "Baking", "Booping", "Brewing",
    "Calculating", "Cerebrating", "Channelling", "Churning", "Clauding", "Coalescing",
    "Cogitating", "Combobulating", "Computing", "Concocting", "Conjuring", "Considering",
    "Contemplating", "Cooking", "Crafting", "Creating", "Crunching", "Deciphering",
    "Deliberating", "Determining", "Discombobulating", "Divining", "Doing", "Effecting",
    "Elucidating", "Enchanting", "Envisioning", "Finagling", "Flibbertigibbeting",
    "Forging", "Forming", "Frolicking", "Generating", "Germinating", "Hatching",
    "Herding", "Honking", "Hustling", "Ideating", "Imagining", "Incubating", "Inferring",
    "Jiving", "Manifesting", "Marinating", "Meandering", "Moseying", "Mulling",
    "Mustering", "Musing", "Noodling", "Percolating", "Perusing", "Philosophising",
    "Pondering", "Pontificating", "Processing", "Puttering", "Puzzling", "Reticulating",
    "Ruminating", "Scheming", "Schlepping", "Shimmying", "Shucking", "Simmering",
    "Smooshing", "Spelunking", "Spinning", "Stewing", "Sussing", "Synthesizing",
    "Thinking", "Tinkering", "Transmuting", "Unfurling", "Unravelling", "Vibing",
    "Wandering", "Whirring", "Wibbling", "Wizarding", "Working", "Wrangling",
]

def get_thinking_message() -> str:
    """Get a random thinking message with emoji."""
    verb = random.choice(SPINNER_VERBS)
    return f"✨ <i>{verb}...</i>"

def get_continue_message() -> str:
    """Get a random continue message with emoji."""
    verb = random.choice(SPINNER_VERBS)
    return f"🔄 <i>{verb}...</i>"

from . import telegram
from .claude import sessions, ClaudeResult, PermissionDenial, get_session_permission_mode, list_recent_sessions, read_session_messages, find_session_working_dir
from .config import settings
from .markdown import markdown_to_telegram_html
from .tunnel import tunnel, CloudflareTunnel
from .topic import generate_provisional_name, extract_title_from_response, generate_title_fallback, format_topic_name, working_dir_name

# Store pending permission requests for retry
pending_permissions: dict[str, dict] = {}  # chat_id -> {message, denials, session_key, bot_name}

# Store pending voice transcription texts (callback_data limited to 64 bytes)
pending_voice_texts: dict[str, str] = {}  # chat_id -> full transcription text

# Store working_dir for resume callbacks (callback_data too small for full path)
resume_working_dirs: dict[str, str] = {}  # session_id -> working_dir

# Bot configurations (initialized at startup)
bots: dict[str, BotConfig] = {}

# Map chat_id -> bot_name for routing notifications
chat_to_bot: dict[str, str] = {}

# Polling tasks
polling_tasks: list[asyncio.Task] = []

def get_runner(bot: BotConfig, thread_id: int = 0):
    """Get the runner for a bot + thread combination.

    For the dev bot (no fixed_working_dir), a thread may have been created
    in a specific directory via /resume. Check existing sessions first.
    """
    if thread_id and not bot.fixed_working_dir:
        existing = sessions.find_by_thread(thread_id)
        if existing:
            return existing
    working_dir = bot.fixed_working_dir or sessions.default_dir
    return sessions.get_session(working_dir, thread_id=thread_id)

get_runner_for_bot = get_runner  # Backward compat


def build_session_buttons(session_list: list, current) -> dict:
    """Build inline keyboard buttons for session selection."""
    buttons = []
    row = []
    for i, (dir_key, session) in enumerate(session_list, 1):
        # Mark current session with checkmark
        label = f"{'✓ ' if session == current else ''}{i}. {session.short_name}"
        row.append({"text": label, "callback_data": f"dir:{dir_key}"})
        # Max 2 buttons per row
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return {"inline_keyboard": buttons}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Current tunnel URL (if using tunnel mode)
tunnel_url: str | None = None


async def poll_updates(bot: BotConfig):
    """Poll Telegram for updates for a specific bot."""
    offset = 0
    logger.info(f"Starting polling for bot '{bot.name}'...")

    while True:
        try:
            updates = await telegram.get_updates(offset=offset, timeout=30, api_url=bot.api_url)

            for update in updates:
                offset = update["update_id"] + 1

                if "message" in update:
                    await handle_message(update["message"], bot)
                elif "callback_query" in update:
                    await handle_callback(update["callback_query"], bot)

        except asyncio.CancelledError:
            logger.info(f"Polling stopped for bot '{bot.name}'")
            break
        except Exception as e:
            logger.error(f"Polling error ({bot.name}): {e}")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Setup and teardown."""
    global bots, chat_to_bot, polling_tasks, tunnel_url

    # Initialize bots
    bots = create_bots()
    for bot_name, bot in bots.items():
        chat_to_bot[str(bot.chat_id)] = bot_name
        # Fetch bot username via getMe
        try:
            me = await telegram.get_me(api_url=bot.api_url)
            bot.username = me.get("result", {}).get("username")
            logger.info(f"Bot {bot_name}: @{bot.username}")
        except Exception as e:
            logger.warning(f"Failed to fetch username for {bot_name}: {e}")
    logger.info(f"Initialized bots: {list(bots.keys())}")

    mode = settings.mode

    # Tunnel mode — only for dev bot
    if mode == "tunnel":
        if not CloudflareTunnel.is_available():
            logger.warning("cloudflared not found, falling back to polling mode")
            mode = "polling"
        else:
            logger.info("Starting Cloudflare tunnel...")
            tunnel.port = settings.port
            tunnel_url = await tunnel.start()

            if tunnel_url:
                webhook_url = f"{tunnel_url}{settings.webhook_path}"
                logger.info(f"Tunnel URL: {tunnel_url}")
                logger.info(f"Setting webhook: {webhook_url}")
                try:
                    await telegram.set_webhook_with_retry(webhook_url, api_url=bots["dev"].api_url)
                    logger.info("Webhook set successfully")
                except Exception as e:
                    logger.error(f"Webhook setup failed after retries: {e}, falling back to polling")
                    mode = "polling"
            else:
                logger.warning("Tunnel failed to start, falling back to polling mode")
                mode = "polling"

    # Manual webhook mode
    if mode == "webhook" and settings.webhook_url:
        webhook_url = f"{settings.webhook_url}{settings.webhook_path}"
        logger.info(f"Setting webhook: {webhook_url}")
        await telegram.set_webhook(webhook_url, api_url=bots["dev"].api_url)

    # Polling mode (fallback or default)
    if mode == "polling":
        logger.info("Starting polling mode...")
        for bot_name, bot in bots.items():
            await telegram.delete_webhook(api_url=bot.api_url)
            task = asyncio.create_task(poll_updates(bot))
            polling_tasks.append(task)

    yield

    # Cleanup
    for task in polling_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    if tunnel.is_running:
        await telegram.delete_webhook(api_url=bots["dev"].api_url)
        await tunnel.stop()

    if mode == "webhook" and settings.webhook_url:
        await telegram.delete_webhook(api_url=bots["dev"].api_url)



app = FastAPI(title="Claude Telegram", lifespan=lifespan)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "claude_running": sessions.any_running(),
        "active_sessions": sum(len(threads) for threads in sessions.sessions.values()),
        "active_dirs": len(sessions.sessions),
    }


@app.post(settings.webhook_path)
async def webhook(request: Request):
    """Handle Telegram webhook updates (dev bot only in tunnel/webhook mode)."""
    data = await request.json()
    logger.info(f"Received update: {data}")

    dev_bot = bots.get("dev")
    if not dev_bot:
        return {"ok": False}

    if "message" in data:
        await handle_message(data["message"], dev_bot)
    elif "callback_query" in data:
        await handle_callback(data["callback_query"], dev_bot)

    return {"ok": True}


async def handle_message(message: dict, bot: BotConfig):
    """Process incoming Telegram message."""
    chat_id = str(message["chat"]["id"])
    thread_id = message.get("message_thread_id")
    is_topic_message = message.get("is_topic_message", False)

    text_preview = (message.get("text") or "")[:50]
    logger.info(f"handle_message: text={text_preview!r}, thread_id={thread_id}, is_topic={is_topic_message}, bot={bot.name}")

    if not bot.is_authorized(chat_id):
        logger.warning(f"Unauthorized access from chat_id: {chat_id} on bot {bot.name}")
        return

    # Handle voice messages
    voice = message.get("voice") or message.get("audio")
    if voice:
        await handle_voice(message, bot, thread_id=thread_id)
        return

    # Handle photo messages (compressed photos or image documents)
    photo = message.get("photo")
    document = message.get("document")
    if photo or (document and document.get("mime_type", "").startswith("image/")):
        await handle_photo(message, bot, thread_id=thread_id)
        return

    text = message.get("text", "")
    if not text:
        return

    # Handle commands
    if text.startswith("/"):
        await handle_command(text, chat_id, bot, thread_id=thread_id, is_topic_message=is_topic_message)
        return

    # --- Topic routing ---
    # If message is in General (not a topic message), create a new topic
    topic_just_created = False
    if not is_topic_message:
        thread_id = await _create_topic_for_message(text, chat_id, bot)
        topic_just_created = True

    # Route to handler
    runner = get_runner(bot, thread_id=thread_id or 0)
    continue_session = runner.is_in_conversation() or is_quick_reply(text)

    await run_claude(text, chat_id, bot, continue_session=continue_session, thread_id=thread_id, new_session=topic_just_created)


async def _create_topic_for_message(text: str, chat_id: str, bot: BotConfig) -> int:
    """Create a new topic for a message sent in General."""
    if bot.fixed_working_dir:
        name = generate_provisional_name(text, is_agent=True)
    else:
        name = generate_provisional_name(text, dir_name=working_dir_name(sessions.default_dir))
    try:
        result = await telegram.create_forum_topic(chat_id, name, api_url=bot.api_url)
        thread_id = result["result"]["message_thread_id"]
        logger.info(f"Created topic '{name}' (thread_id={thread_id})")
        return thread_id
    except Exception as e:
        logger.error(f"Failed to create topic: {e}")
        raise


async def _send_dir_browser(
    rel_path: str, chat_id: str, bot: BotConfig, thread_id: int | None,
    edit_message_id: int | None = None,
):
    """Send (or edit) a directory browser with clickable buttons for subdirectories.

    If edit_message_id is provided, edits that message in-place instead of sending a new one.
    """
    home = Path.home()
    browse_dir = home / rel_path if rel_path else home

    if not browse_dir.is_dir():
        text = f"❌ Not found: <code>{html.escape(rel_path)}</code>"
        if edit_message_id:
            await telegram.edit_message(edit_message_id, text, chat_id=chat_id, parse_mode="HTML", api_url=bot.api_url)
        else:
            await telegram.send_message(text, chat_id=chat_id, parse_mode="HTML", api_url=bot.api_url, message_thread_id=thread_id)
        return

    # List subdirectories (skip hidden dirs and common noise)
    skip = {".", "..", "__pycache__", "node_modules", ".git", ".venv", "venv", ".cache", ".local", ".config", ".npm", ".nvm"}
    try:
        subdirs = sorted(
            d.name for d in browse_dir.iterdir()
            if d.is_dir() and d.name not in skip and not d.name.startswith(".")
        )
    except PermissionError:
        subdirs = []

    current_name = Path(sessions.default_dir).name
    display_path = f"~/{rel_path}" if rel_path else "~"

    buttons = []
    # Navigation: back button if not at root
    if rel_path:
        parent = str(Path(rel_path).parent)
        if parent == ".":
            parent = ""
        buttons.append([{"text": "⬆️ ..", "callback_data": f"browse:{parent}"}])
        # Select this directory button
        buttons.append([{"text": f"✅ Select {browse_dir.name}", "callback_data": f"dir:{rel_path}"}])
    else:
        # At root: offer to stay in current dir
        buttons.append([{"text": f"✅ Stay in {current_name}", "callback_data": "dir:_stay"}])

    # Subdirectory buttons (2 per row, max 20)
    row = []
    for name in subdirs[:20]:
        child_path = f"{rel_path}/{name}" if rel_path else name
        # callback_data max 64 bytes — truncate if needed
        cb = f"browse:{child_path}"
        if len(cb.encode()) > 64:
            continue
        row.append({"text": f"📁 {name}", "callback_data": cb})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    text = (
        f"📂 <code>{html.escape(display_path)}</code>\n"
        f"📍 Current: <code>{html.escape(current_name)}</code>"
    )
    markup = {"inline_keyboard": buttons}

    if not subdirs and not rel_path:
        text = f"📂 <code>{html.escape(display_path)}</code> — no subdirectories"
        markup = None

    if edit_message_id:
        await telegram.edit_message(
            edit_message_id, text, chat_id=chat_id, parse_mode="HTML",
            api_url=bot.api_url, reply_markup=markup,
        )
    else:
        await telegram.send_message(
            text, chat_id=chat_id, parse_mode="HTML",
            reply_markup=markup, api_url=bot.api_url,
            message_thread_id=thread_id,
        )


async def _resume_session(
    session_id: str,
    message: str,
    messages: list[dict],
    working_dir: str,
    chat_id: str,
    bot: BotConfig,
    thread_id: int | None,
    is_topic_message: bool,
    source_message_id: int | None = None,
):
    """Resume a specific Claude session — create topic, show recap, run Claude.

    If source_message_id is provided, edits that message to replace the button
    with a clickable link to the new topic (edit-in-place UX).
    """
    # Create topic if not already in one
    if not is_topic_message:
        # Use first user message as topic name
        first_msg = next((m["text"] for m in messages if m["role"] == "user"), message)
        dir_name = working_dir_name(working_dir)
        name = generate_provisional_name(first_msg, dir_name=dir_name, is_agent=False)
        try:
            result = await telegram.create_forum_topic(chat_id, name, api_url=bot.api_url)
            thread_id = result["result"]["message_thread_id"]
        except Exception as e:
            logger.error(f"Failed to create topic for resume: {e}")
            await telegram.send_message(
                f"❌ Failed to create topic: {html.escape(str(e))}",
                chat_id=chat_id, parse_mode="HTML", api_url=bot.api_url,
            )
            return

    # Show message recap in the topic first (this triggers Telegram's native
    # "Continue to last topic" button for the user to navigate)
    recap_lines = []
    for m in messages:
        text = m["text"][:200].replace("\n", " ")
        if len(m["text"]) > 200:
            text += "…"
        if m["role"] == "user":
            recap_lines.append(f"👤 <b>{html.escape(text)}</b>")
        else:
            recap_lines.append(f"🤖 <i>{html.escape(text)}</i>")

    if recap_lines:
        recap = "\n".join(recap_lines)
        await telegram.send_message(
            f"📜 <b>Session resumed</b> (<code>{session_id[:8]}…</code>)\n\n{recap}",
            chat_id=chat_id, parse_mode="HTML",
            api_url=bot.api_url, message_thread_id=thread_id,
        )

    # Update the General message with confirmation + "Go to topic" button
    dir_name = working_dir_name(working_dir)
    general_text = f"✅ <b>Session resumed</b> (<code>{html.escape(dir_name)}</code>)"
    goto_markup = {"inline_keyboard": [[
        {"text": "Go to topic ➜", "callback_data": f"goto:{thread_id}"},
    ]]}
    if source_message_id:
        try:
            await telegram.edit_message(
                source_message_id, general_text,
                chat_id=chat_id, parse_mode="HTML", api_url=bot.api_url,
                reply_markup=goto_markup,
            )
        except Exception as e:
            logger.warning(f"Failed to edit source message: {e}")
            await telegram.send_message(
                general_text, chat_id=chat_id, parse_mode="HTML", api_url=bot.api_url,
                reply_markup=goto_markup,
            )
    else:
        await telegram.send_message(
            general_text, chat_id=chat_id, parse_mode="HTML", api_url=bot.api_url,
            reply_markup=goto_markup,
        )

    # Set session_id on the runner so next message in this topic continues the session
    runner = sessions.get_session(working_dir, thread_id=thread_id or 0)
    runner.session_id = session_id
    runner.context_shown = True  # Recap already shown above, skip duplicate


async def handle_voice(message: dict, bot: BotConfig, *, thread_id: int | None = None):
    """Handle voice/audio messages — transcribe and offer to process."""
    chat_id = str(message["chat"]["id"])
    is_topic_message = message.get("is_topic_message", False)
    voice = message.get("voice") or message.get("audio")
    file_id = voice["file_id"]

    # Topic routing: create topic if message is in General
    topic_just_created = False
    if not is_topic_message:
        thread_id = await _create_topic_for_message("Message vocal", chat_id, bot)
        topic_just_created = True

    await telegram.send_message(
        "🎤 <i>Transcription en cours...</i>",
        chat_id=chat_id,
        parse_mode="HTML",
        api_url=bot.api_url,
        message_thread_id=thread_id,
    )

    try:
        # Download file from Telegram
        file_info = await telegram.get_file(file_id, api_url=bot.api_url)
        file_path = file_info["result"]["file_path"]
        audio_data = await telegram.download_file(file_path, api_url=bot.api_url)

        # Save to temp file
        import tempfile
        suffix = Path(file_path).suffix or ".ogg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_data)
            tmp_path = tmp.name

        # Transcribe
        result = await transcribe_audio(tmp_path)

        # Cleanup temp file
        Path(tmp_path).unlink(missing_ok=True)

        # Show transcription with button to process
        # Store full text in memory (callback_data limited to 64 bytes)
        pending_voice_texts[chat_id] = f"[Transcription vocale ({result.duration_formatted}, {result.engine})]\n\n{result.text}"
        buttons = {"inline_keyboard": [[
            {"text": "✅ Send to Claude", "callback_data": "voice:send"},
        ]]}
        full_text = f"🎤 <b>Transcription</b> ({html.escape(result.duration_formatted)})\n\n{html.escape(result.text)}"
        chunks = split_text(full_text, 4000)
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            await telegram.send_message(
                chunk,
                chat_id=chat_id,
                parse_mode="HTML",
                reply_markup=buttons if is_last else None,
                api_url=bot.api_url,
                message_thread_id=thread_id,
            )
            if not is_last:
                await asyncio.sleep(0.3)

    except Exception as e:
        logger.exception("Transcription error")
        await telegram.send_message(
            f"❌ Transcription failed: <code>{html.escape(str(e))}</code>",
            chat_id=chat_id,
            parse_mode="HTML",
            api_url=bot.api_url,
            message_thread_id=thread_id,
        )


async def handle_photo(message: dict, bot: BotConfig, *, thread_id: int | None = None):
    """Handle photo/image messages — download and send to Claude for vision analysis."""
    chat_id = str(message["chat"]["id"])
    is_topic_message = message.get("is_topic_message", False)
    caption = message.get("caption", "")

    # Topic routing: create topic if message is in General
    topic_just_created = False
    if not is_topic_message:
        thread_id = await _create_topic_for_message(caption or "Image", chat_id, bot)
        topic_just_created = True

    # Get file_id: photo array (take largest) or document
    photo = message.get("photo")
    document = message.get("document")
    if photo:
        file_id = photo[-1]["file_id"]  # Largest resolution
    else:
        file_id = document["file_id"]

    try:
        # Download file from Telegram
        file_info = await telegram.get_file(file_id, api_url=bot.api_url)
        file_path = file_info["result"]["file_path"]
        image_data = await telegram.download_file(file_path, api_url=bot.api_url)

        # Save to temp file
        import tempfile
        suffix = Path(file_path).suffix or ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, prefix="claude_photo_") as tmp:
            tmp.write(image_data)
            tmp_path = tmp.name

        # Build prompt with image path
        user_text = caption or "Analyse cette image."
        image_prompt = f"[Image jointe : {tmp_path}]\n\n{user_text}"

        # Direct execution
        try:
            runner = get_runner(bot, thread_id=thread_id or 0)
            continue_session = runner.is_in_conversation()
            await run_claude(image_prompt, chat_id, bot, continue_session=continue_session, thread_id=thread_id, new_session=topic_just_created)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except Exception as e:
        logger.exception("Photo processing error")
        await telegram.send_message(
            f"❌ Erreur traitement image: <code>{e}</code>",
            chat_id=chat_id,
            parse_mode="HTML",
            api_url=bot.api_url,
            message_thread_id=thread_id,
        )


def is_quick_reply(text: str) -> bool:
    """Check if the message is a quick reply (number, yes/no, etc.)."""
    text = text.strip().lower()
    # Single number
    if re.match(r"^\d+$", text):
        return True
    # Common quick replies
    if text in ("yes", "no", "y", "n", "ok", "cancel", "skip", "done", "next"):
        return True
    return False


async def handle_command(text: str, chat_id: str, bot: BotConfig, *, thread_id: int | None = None, is_topic_message: bool = False):
    """Handle bot commands."""
    cmd = text.split()[0].lower()
    args = text[len(cmd):].strip()

    # Check command whitelist for this bot
    if cmd not in bot.commands_whitelist:
        await telegram.send_message(
            f"Commande inconnue — tape <code>/help</code> pour voir les commandes",
            chat_id=chat_id,
            parse_mode="HTML",
            api_url=bot.api_url,
            message_thread_id=thread_id,
        )
        return

    if cmd == "/start" or cmd == "/help":
        await telegram.send_message(
            "<b>Claude Code</b> via Telegram\n\n"
            "<b>Commands</b>\n"
            "<code>/c &lt;msg&gt;</code> — Continue conversation\n"
            "<code>/new &lt;msg&gt;</code> — Fresh session\n"
            "<code>/resume</code> — Resume a previous session\n"
            "<code>/dir path</code> — Switch directory (relative to ~)\n"
            "<code>/dirs</code> — List sessions + buttons\n"
            "<code>/repos</code> — Favorite repos\n"
            "<code>/rmdir path</code> — Remove a session\n"
            "<code>/compact</code> — Compact context\n"
            "<code>/cancel</code> — Stop current task\n"
            "<code>/status</code> — Check status\n\n"
            "<b>Tips</b>\n"
            "• Just type to chat — auto-continues for 10 min\n"
            "• <code>/dir projects/foo</code> = ~/projects/foo\n"
            "• Tap buttons in /repos to start in a repo",
            chat_id=chat_id,
            parse_mode="HTML",
            api_url=bot.api_url,
            message_thread_id=thread_id,
        )

    elif cmd == "/c" or cmd == "/continue":
        if args:
            await run_claude(args, chat_id, bot, continue_session=True, thread_id=thread_id)
        else:
            await telegram.send_message(
                "Usage: <code>/c &lt;message&gt;</code>",
                chat_id=chat_id,
                parse_mode="HTML",
                api_url=bot.api_url,
                message_thread_id=thread_id,
            )

    elif cmd == "/resume":
        working_dir = bot.fixed_working_dir or sessions.default_dir
        if args:
            # Direct resume: /resume <session_id> [optional message]
            parts = args.split(None, 1)
            session_id = parts[0]
            message = parts[1] if len(parts) > 1 else "Continue."

            messages = read_session_messages(session_id, working_dir)
            if messages is None:
                await telegram.send_message(
                    f"❌ Session introuvable : <code>{html.escape(session_id[:40])}</code>",
                    chat_id=chat_id, parse_mode="HTML",
                    api_url=bot.api_url, message_thread_id=thread_id,
                )
                return

            await _resume_session(session_id, message, messages, working_dir, chat_id, bot, thread_id, is_topic_message)
        else:
            # Session picker: /resume (no args)
            recent = list_recent_sessions(working_dir)
            if not recent:
                await telegram.send_message(
                    "❌ Aucune session trouvée pour ce répertoire.",
                    chat_id=chat_id, parse_mode="HTML",
                    api_url=bot.api_url, message_thread_id=thread_id,
                )
                return

            dir_name = Path(working_dir).name
            buttons = []
            for s in recent:
                ts = s["timestamp"]
                # Parse ISO timestamp to show date + time
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    label = dt.strftime("%d/%m %H:%M")
                except (ValueError, AttributeError):
                    label = "?"
                # Truncate first message for button text
                msg_preview = s["first_message"][:40].replace("\n", " ")
                if len(s["first_message"]) > 40:
                    msg_preview += "…"
                buttons.append([{
                    "text": f"{label} — {msg_preview}",
                    "callback_data": f"resume:{s['id']}",
                }])

            await telegram.send_message(
                f"📂 <b>{html.escape(dir_name)}</b> — Sessions récentes :\n\n"
                "<i>Sélectionne une session à reprendre :</i>",
                chat_id=chat_id, parse_mode="HTML",
                reply_markup={"inline_keyboard": buttons},
                api_url=bot.api_url, message_thread_id=thread_id,
            )

    elif cmd == "/new":
        if args:
            if is_topic_message and thread_id:
                # In a topic: reset session for that thread
                runner = get_runner(bot, thread_id=thread_id)
                runner.last_interaction = None
                await run_claude(args, chat_id, bot, continue_session=False, thread_id=thread_id)
            else:
                # In General: create a new topic
                thread_id = await _create_topic_for_message(args, chat_id, bot)
                await run_claude(args, chat_id, bot, continue_session=False, thread_id=thread_id)
        else:
            await telegram.send_message(
                "Usage: <code>/new &lt;message&gt;</code>",
                chat_id=chat_id,
                parse_mode="HTML",
                api_url=bot.api_url,
                message_thread_id=thread_id,
            )

    elif cmd == "/dir":
        if args:
            session = sessions.switch_session(args)
            status = "🔄 running" if session.is_running else "💤 idle"
            conv = "in conversation" if session.is_in_conversation() else "fresh"

            # Check for stored session context
            context = None
            if not session.context_shown and not session.is_in_conversation():
                context = session.get_session_context()

            msg = f"📂 Switched to <code>{session.short_name}</code>"
            if context:
                msg += f"\n\n📜 <b>Previous session:</b>\n<i>{context}</i>"
            msg += "\n\n<code>/resume</code> to resume a session\nor send a message to start a new one"

            await telegram.send_message(
                msg,
                chat_id=chat_id,
                parse_mode="HTML",
                api_url=bot.api_url,
                message_thread_id=thread_id,
            )
        else:
            # Browse directories starting from home
            await _send_dir_browser("", chat_id, bot, thread_id)

    elif cmd == "/dirs":
        dir_list = sessions.list_dirs()
        if not dir_list:
            await telegram.send_message(
                "No active sessions",
                chat_id=chat_id,
                parse_mode="HTML",
                api_url=bot.api_url,
                message_thread_id=thread_id,
            )
        else:
            lines = ["<b>Active Directories</b>\n"]
            for i, (dir_key, thread_count) in enumerate(dir_list, 1):
                short = Path(dir_key).name
                lines.append(f"{i}. 📂 <code>{short}</code> ({thread_count} topic{'s' if thread_count != 1 else ''})")
            await telegram.send_message(
                "\n".join(lines),
                chat_id=chat_id,
                parse_mode="HTML",
                api_url=bot.api_url,
                message_thread_id=thread_id,
            )

    elif cmd == "/compact":
        runner = get_runner(bot, thread_id=thread_id or 0)
        if runner.is_running:
            await telegram.send_message(
                "⏳ Claude is busy — use <code>/cancel</code> first",
                chat_id=chat_id,
                parse_mode="HTML",
                api_url=bot.api_url,
                message_thread_id=thread_id,
            )
            return
        await telegram.send_message(
            f"🗜 <i>Compacting context for {runner.short_name}...</i>",
            chat_id=chat_id,
            parse_mode="HTML",
            api_url=bot.api_url,
            message_thread_id=thread_id,
        )
        result = await runner.compact()
        await send_response(result.text, chat_id, api_url=bot.api_url, message_thread_id=thread_id)

    elif cmd == "/cancel":
        runner = get_runner(bot, thread_id=thread_id or 0)
        cancelled = await runner.cancel()
        drained = 0
        if cancelled or drained:
            msg = f"🛑 Cancelled <code>{runner.short_name}</code>"
            await telegram.send_message(msg, chat_id=chat_id, parse_mode="HTML", api_url=bot.api_url, message_thread_id=thread_id)
        else:
            await telegram.send_message("Nothing to cancel", chat_id=chat_id, parse_mode="HTML", api_url=bot.api_url, message_thread_id=thread_id)

    elif cmd == "/status":
        runner = get_runner(bot, thread_id=thread_id or 0)
        if runner.is_running:
            status = "🔄 <b>Running</b>"
        else:
            status = "💤 <b>Idle</b>"
        conv = "in conversation" if runner.is_in_conversation() else "new session"
        msg = f"📂 <code>{runner.short_name}</code>\n{status} • {conv}"
        await telegram.send_message(msg, chat_id=chat_id, parse_mode="HTML", api_url=bot.api_url, message_thread_id=thread_id)

    elif cmd == "/rmdir":
        if args:
            if sessions.remove_session(args):
                current = get_runner(bot, thread_id=thread_id or 0)
                await telegram.send_message(
                    f"🗑 Removed session <code>{args}</code>\n"
                    f"📍 Current: <code>{current.short_name}</code>",
                    chat_id=chat_id,
                    parse_mode="HTML",
                    api_url=bot.api_url,
                    message_thread_id=thread_id,
                )
            else:
                await telegram.send_message(
                    f"❌ Could not remove <code>{args}</code>\n"
                    "<i>(Session not found or currently running)</i>",
                    chat_id=chat_id,
                    parse_mode="HTML",
                    api_url=bot.api_url,
                    message_thread_id=thread_id,
                )
        else:
            await telegram.send_message(
                "Usage: <code>/rmdir path</code>",
                chat_id=chat_id,
                parse_mode="HTML",
                api_url=bot.api_url,
                message_thread_id=thread_id,
            )

    elif cmd == "/repos":
        favorites = settings.get_favorite_repos()
        if not favorites:
            await telegram.send_message(
                "No favorite repos configured.\n\n"
                "Add <code>FAVORITE_REPOS</code> to your .env:\n"
                "<code>FAVORITE_REPOS=projects/foo,projects/bar</code>",
                chat_id=chat_id,
                parse_mode="HTML",
                api_url=bot.api_url,
                message_thread_id=thread_id,
            )
        else:
            # Build buttons for favorite repos
            current = get_runner(bot, thread_id=thread_id or 0)
            buttons = []
            row = []
            for repo in favorites:
                # Use last part of path as label
                label = repo.split("/")[-1]
                row.append({"text": f"📁 {label}", "callback_data": f"repo:{repo}"})
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)

            await telegram.send_message(
                f"<b>Favorite Repos</b>\n"
                f"📍 Current: <code>{current.short_name}</code>\n\n"
                "Select a repo to switch:",
                chat_id=chat_id,
                parse_mode="HTML",
                reply_markup={"inline_keyboard": buttons},
                api_url=bot.api_url,
                message_thread_id=thread_id,
            )

    else:
        # Unknown command - maybe they meant to chat?
        await telegram.send_message(
            f"Unknown command — try <code>/c {text}</code> to continue",
            chat_id=chat_id,
            parse_mode="HTML",
            api_url=bot.api_url,
            message_thread_id=thread_id,
        )


async def handle_callback(callback: dict, bot: BotConfig):
    """Handle callback query from inline buttons."""
    query_id = callback["id"]
    data = callback.get("data", "")
    chat_id = callback["message"]["chat"]["id"]

    logger.info(f"handle_callback: data={data}, chat_id={chat_id}")

    if not bot.is_authorized(chat_id):
        logger.warning(f"Unauthorized callback from {chat_id}")
        return

    # Answer the callback to remove loading state (may fail for stale queries after restart)
    try:
        await telegram.answer_callback(query_id, api_url=bot.api_url)
    except Exception:
        pass

    if data.startswith("goto:"):
        # Send a message in the target topic to trigger Telegram's native
        # "Continue to last topic" button in the user's current view
        try:
            target_thread = int(data.split(":", 1)[1])
            await telegram.send_message(
                "⬆️ <i>Topic is ready — type your message here</i>",
                chat_id=str(chat_id), parse_mode="HTML", api_url=bot.api_url,
                message_thread_id=target_thread,
            )
        except Exception:
            pass
        return

    if data.startswith("reply:"):
        reply = data[6:]  # Remove "reply:" prefix
        callback_thread_id = callback["message"].get("message_thread_id", 0)
        await run_claude(reply, str(chat_id), bot, continue_session=True, thread_id=callback_thread_id)

    elif data.startswith("voice:"):
        voice_text = pending_voice_texts.pop(str(chat_id), None)
        if voice_text:
            callback_thread_id = callback["message"].get("message_thread_id", 0)
            await run_claude(voice_text, str(chat_id), bot, continue_session=False, thread_id=callback_thread_id)
        else:
            await telegram.send_message(
                "⚠️ Transcription expirée, renvoie le message vocal.",
                chat_id=str(chat_id), parse_mode="HTML", api_url=bot.api_url,
            )

    elif data.startswith("browse:"):
        rel_path = data.split(":", 1)[1]
        msg_id = callback["message"]["message_id"]
        await _send_dir_browser(rel_path, str(chat_id), bot, thread_id=None, edit_message_id=msg_id)

    elif data.startswith("dir:") or data.startswith("repo:"):
        # Handle both dir: and repo: callbacks the same way
        dir_path = data.split(":", 1)[1]  # Remove prefix
        msg_id = callback["message"]["message_id"]

        if dir_path == "_stay":
            # User chose to stay in current directory
            current_name = Path(sessions.default_dir).name
            msg = (
                f"📂 Staying in <code>{html.escape(current_name)}</code>\n\n"
                f"<code>/resume</code> to resume a session\nor send a message to start a new one"
            )
            await telegram.edit_message(
                msg_id, msg, chat_id=str(chat_id), parse_mode="HTML", api_url=bot.api_url,
            )
            return

        session = sessions.switch_session(dir_path)
        status = "🔄 running" if session.is_running else "💤 idle"
        conv = "in conversation" if session.is_in_conversation() else "fresh"

        # Check for stored session context
        context = None
        if not session.context_shown and not session.is_in_conversation():
            context = session.get_session_context()

        msg = f"📂 Switched to <code>{session.short_name}</code>"
        if context:
            msg += f"\n\n📜 <b>Previous session:</b>\n<i>{context}</i>"
        msg += "\n\n<code>/resume</code> to resume a session\nor send a message to start a new one"

        # Edit the browser message in-place with confirmation
        await telegram.edit_message(
            msg_id, msg, chat_id=str(chat_id), parse_mode="HTML", api_url=bot.api_url,
        )

    elif data.startswith("resume:"):
        session_id = data.split(":", 1)[1]
        msg_id = callback["message"]["message_id"]
        # Use stored working_dir from notification, fall back to scanning all projects
        working_dir = resume_working_dirs.pop(session_id, None)
        source = "resume_working_dirs"
        if not working_dir:
            working_dir = find_session_working_dir(session_id)
            source = "find_session_working_dir"
        if not working_dir:
            working_dir = bot.fixed_working_dir or sessions.default_dir
            source = "fallback"
        logger.info(f"resume: session_id={session_id}, working_dir={working_dir} (source={source})")
        messages = read_session_messages(session_id, working_dir, last_n=10)
        if messages is None:
            await telegram.send_message(
                f"❌ Session not found: <code>{html.escape(session_id[:40])}</code>",
                chat_id=str(chat_id), parse_mode="HTML", api_url=bot.api_url,
            )
            return

        await _resume_session(
            session_id, "Continue.", messages, working_dir,
            str(chat_id), bot, thread_id=None, is_topic_message=False,
            source_message_id=msg_id,
        )

    elif data == "perm:allow":
        # User approved the permission request - retry with allowed tools
        logger.info(f"perm:allow clicked, pending_permissions: {pending_permissions}")
        pending = pending_permissions.get(str(chat_id))
        if not pending:
            await telegram.send_message(
                "No pending permission request.",
                chat_id=chat_id,
                parse_mode="HTML",
                api_url=bot.api_url,
            )
            return

        # Build allowed tools list from denials
        # Format: "Tool" or "Bash(pattern:*)" for command matching
        allowed_tools = []
        for denial in pending["denials"]:
            tool = denial.tool_name
            tool_input = denial.tool_input
            if tool in ("Write", "Edit", "Read"):
                # For file tools, just allow the tool (can't filter by path)
                allowed_tools.append(tool)
            elif tool == "Bash":
                # For Bash, try to match the specific command
                cmd = tool_input.get("command", "")
                # Extract first word of command for pattern matching
                first_word = cmd.split()[0] if cmd.split() else ""
                if first_word:
                    allowed_tools.append(f"Bash({first_word}:*)")
                else:
                    allowed_tools.append("Bash")
            else:
                allowed_tools.append(tool)

        # Clear pending and retry
        original_message = pending["message"]
        del pending_permissions[str(chat_id)]

        await telegram.send_message(
            f"✅ <i>Retrying with permissions...</i>",
            chat_id=chat_id,
            parse_mode="HTML",
            api_url=bot.api_url,
        )

        callback_thread_id = callback["message"].get("message_thread_id", 0)
        await run_claude(
            original_message,
            str(chat_id),
            bot,
            continue_session=True,
            allowed_tools=allowed_tools,
            thread_id=callback_thread_id,
        )

    elif data == "perm:deny":
        # User denied - just clear the pending request
        if str(chat_id) in pending_permissions:
            del pending_permissions[str(chat_id)]
        await telegram.send_message(
            "❌ Permission denied. Request cancelled.",
            chat_id=chat_id,
            parse_mode="HTML",
            api_url=bot.api_url,
        )

    elif data == "perm:bypass":
        # User wants to continue with bypass permissions
        logger.info(f"perm:bypass clicked, pending_permissions: {pending_permissions}")
        pending = pending_permissions.get(str(chat_id))
        if not pending:
            await telegram.send_message(
                "No pending permission request.",
                chat_id=chat_id,
                parse_mode="HTML",
                api_url=bot.api_url,
            )
            return

        # Clear pending and retry with bypass
        original_message = pending["message"]
        del pending_permissions[str(chat_id)]

        await telegram.send_message(
            f"🔓 <i>Retrying with bypass permissions...</i>",
            chat_id=chat_id,
            parse_mode="HTML",
            api_url=bot.api_url,
        )

        # Retry with bypass permissions
        callback_thread_id = callback["message"].get("message_thread_id", 0)
        await run_claude(original_message, str(chat_id), bot, continue_session=True, bypass_permissions=True, thread_id=callback_thread_id)


async def animate_status(chat_id: str, message_id: int, continue_session: bool, session_name: str, api_url: str | None = None, message_thread_id: int | None = None):
    """Animate the status message with rotating messages."""
    prefix = f"[<code>{session_name}</code>] " if session_name != "default" else ""
    try:
        while True:
            await asyncio.sleep(2.5)  # Update every 2.5 seconds
            status = get_continue_message() if continue_session else get_thinking_message()
            new_status = f"{prefix}{status}"
            try:
                await telegram.edit_message(message_id, new_status, chat_id, parse_mode="HTML", api_url=api_url)
            except Exception:
                pass  # Ignore edit errors (message may be deleted)
    except asyncio.CancelledError:
        pass


async def run_claude(
    message: str,
    chat_id: str,
    bot: BotConfig,
    continue_session: bool = False,
    allowed_tools: list[str] | None = None,
    bypass_permissions: bool = False,
    thread_id: int | None = None,
    new_session: bool = False,
    working_dir: str | None = None,
):
    """Run Claude and send response to Telegram."""
    if working_dir:
        runner = sessions.get_session(working_dir, thread_id=thread_id or 0)
    else:
        runner = get_runner(bot, thread_id=thread_id or 0)
    session_name = runner.short_name
    prefix = f"[<code>{session_name}</code>] " if session_name != "default" else ""

    if runner.is_running:
        await telegram.send_message(
            f"{prefix}⏳ Claude is busy — use <code>/cancel</code> to stop",
            chat_id=chat_id,
            parse_mode="HTML",
            api_url=bot.api_url,
            message_thread_id=thread_id,
        )
        return

    # Check for stored session context on first interaction (only in General, not in topics)
    if not runner.context_shown and not runner.is_in_conversation() and not thread_id:
        context = runner.get_session_context()
        if context:
            await telegram.send_message(
                f"{prefix}📜 <b>Resuming previous session:</b>\n<i>{context}</i>",
                chat_id=chat_id,
                parse_mode="HTML",
                api_url=bot.api_url,
                message_thread_id=thread_id,
            )

    # Send animated status message
    initial_status = get_continue_message() if continue_session else get_thinking_message()
    status_msg = await telegram.send_message(
        f"{prefix}{initial_status}",
        chat_id=chat_id,
        parse_mode="HTML",
        api_url=bot.api_url,
        message_thread_id=thread_id,
    )
    message_id = status_msg.get("result", {}).get("message_id")

    # Start animation task
    animation_task = None
    if message_id:
        animation_task = asyncio.create_task(
            animate_status(chat_id, message_id, continue_session, session_name, api_url=bot.api_url, message_thread_id=thread_id)
        )

    try:
        result = await runner.run(
            message,
            continue_session=continue_session,
            new_session=new_session,
            allowed_tools=allowed_tools,
            bypass_permissions=bypass_permissions,
            system_prompt=bot.system_prompt,
            mcp_config=bot.mcp_config_path,
        )

        # Stop animation
        if animation_task:
            animation_task.cancel()
            try:
                await animation_task
            except asyncio.CancelledError:
                pass

        # Delete status message
        if message_id:
            await telegram.delete_message(chat_id, message_id, api_url=bot.api_url)

        # Check for permission denials
        logger.info(f"Result: text={result.text[:100] if result.text else 'None'}, denials={result.permission_denials}")
        if result.permission_denials:
            await send_permission_request(
                result, message, chat_id, session_name, sessions.current_dir, bot, thread_id=thread_id
            )
        else:
            response_text = result.text

            # Topic rename after first Claude response
            if thread_id and not continue_session:
                cleaned, title = extract_title_from_response(response_text)
                if title:
                    response_text = cleaned
                else:
                    try:
                        title = await generate_title_fallback(message, response_text)
                    except Exception:
                        title = None

                if title:
                    if bot.fixed_working_dir:
                        new_name = format_topic_name(title, is_agent=True)
                    else:
                        new_name = format_topic_name(title, dir_name=working_dir_name(sessions.default_dir))
                    try:
                        await telegram.edit_forum_topic(chat_id, thread_id, new_name, api_url=bot.api_url)
                    except Exception as e:
                        logger.warning(f"Failed to rename topic: {e}")

            await send_response(response_text, chat_id, session_name=session_name, api_url=bot.api_url, message_thread_id=thread_id)

    except Exception as e:
        # Stop animation on error
        if animation_task:
            animation_task.cancel()
            try:
                await animation_task
            except asyncio.CancelledError:
                pass
        if message_id:
            await telegram.delete_message(chat_id, message_id, api_url=bot.api_url)

        logger.exception("Claude error")
        await telegram.send_message(
            f"{prefix}❌ <b>Error:</b> <code>{e}</code>",
            chat_id=chat_id,
            parse_mode="HTML",
            api_url=bot.api_url,
            message_thread_id=thread_id,
        )


async def send_permission_request(
    result: ClaudeResult,
    original_message: str,
    chat_id: str,
    session_name: str,
    session_dir: str,
    bot: BotConfig,
    thread_id: int | None = None,
):
    """Send permission denial info to user with Allow/Deny buttons."""
    prefix = f"[<code>{session_name}</code>] " if session_name != "default" else ""

    # Format the denied permissions
    denial_lines = []
    for d in result.permission_denials:
        tool = d.tool_name
        if tool == "Write":
            path = html.escape(d.tool_input.get("file_path", "unknown"))
            denial_lines.append(f"• <b>Write</b> to <code>{path}</code>")
        elif tool == "Bash":
            cmd = html.escape(d.tool_input.get("command", "unknown")[:60])
            denial_lines.append(f"• <b>Bash</b>: <code>{cmd}</code>")
        elif tool == "Edit":
            path = html.escape(d.tool_input.get("file_path", "unknown"))
            denial_lines.append(f"• <b>Edit</b> <code>{path}</code>")
        elif tool == "Read":
            path = html.escape(d.tool_input.get("file_path", "unknown"))
            denial_lines.append(f"• <b>Read</b> <code>{path}</code>")
        else:
            denial_lines.append(f"• <b>{html.escape(tool)}</b>: {html.escape(str(d.tool_input)[:50])}")

    # Store pending request for retry
    pending_permissions[str(chat_id)] = {
        "message": original_message,
        "denials": result.permission_denials,
        "session_dir": session_dir,
        "bot_name": bot.name,
    }

    # Build message with buttons
    msg = (
        f"{prefix}⚠️ <b>Permission denied:</b>\n"
        + "\n".join(denial_lines)
    )

    # Also show partial result if any
    if result.text.strip():
        msg += f"\n\n<i>{html.escape(result.text[:500])}</i>"

    # Check if original session was in bypass mode
    permission_mode = get_session_permission_mode(session_dir)
    was_bypass = permission_mode == "bypassPermissions"

    # Build buttons - add bypass option if session was originally in bypass mode
    button_row = [
        {"text": "✅ Allow & Retry", "callback_data": "perm:allow"},
        {"text": "❌ Deny", "callback_data": "perm:deny"},
    ]

    buttons = {"inline_keyboard": [button_row]}

    # Add bypass button on second row if session was in bypass mode
    if was_bypass:
        buttons["inline_keyboard"].append([
            {"text": "🔓 Continue with bypass", "callback_data": "perm:bypass"}
        ])
        msg += "\n\n<i>Original session was in bypass mode.</i>"

    try:
        await telegram.send_message(
            msg,
            chat_id=chat_id,
            parse_mode="HTML",
            reply_markup=buttons,
            api_url=bot.api_url,
            message_thread_id=thread_id,
        )
    except Exception:
        # Fallback to plain text if HTML parsing fails
        logger.warning("Permission denial HTML failed, falling back to plain text")
        await telegram.send_message(
            msg,
            chat_id=chat_id,
            parse_mode=None,
            reply_markup=buttons,
            api_url=bot.api_url,
            message_thread_id=thread_id,
        )


async def send_response(text: str, chat_id: str, chunk_size: int = 4000, session_name: str = "default", api_url: str | None = None, message_thread_id: int | None = None):
    """Send Claude's response, with quick-reply buttons if numbered options detected."""
    if not text.strip():
        await telegram.send_message(
            "<i>(no output)</i>",
            chat_id=chat_id,
            parse_mode="HTML",
            api_url=api_url,
            message_thread_id=message_thread_id,
        )
        return

    # Detect numbered options before converting to HTML
    buttons = detect_options(text)

    # Convert markdown to Telegram HTML
    html_text = markdown_to_telegram_html(text)

    # Split into chunks if needed
    chunks = split_text(html_text, chunk_size)

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        reply_markup = buttons if (is_last and buttons) else None
        try:
            await telegram.send_message(
                chunk,
                chat_id=chat_id,
                parse_mode="HTML",
                reply_markup=reply_markup,
                api_url=api_url,
                message_thread_id=message_thread_id,
            )
        except Exception as e:
            # Fallback to plain text if HTML fails
            logger.warning(f"HTML parse failed, falling back to plain text: {e}")
            await telegram.send_message(
                text if len(chunks) == 1 else chunk,
                chat_id=chat_id,
                parse_mode=None,
                reply_markup=reply_markup,
                api_url=api_url,
                message_thread_id=message_thread_id,
            )
        if not is_last:
            await asyncio.sleep(0.5)


def detect_options(text: str) -> dict | None:
    """Detect numbered options (1. Option, 2. Option) and create inline keyboard."""
    # Look for patterns like "1.", "2.", "3." at start of lines
    pattern = r"^(\d+)[\.\)]\s+"
    matches = re.findall(pattern, text, re.MULTILINE)

    if not matches or len(matches) < 2:
        return None

    # Get unique numbers, max 8 buttons
    numbers = sorted(set(matches))[:8]

    # Create inline keyboard with number buttons
    buttons = [[{"text": n, "callback_data": f"reply:{n}"} for n in numbers[:4]]]
    if len(numbers) > 4:
        buttons.append([{"text": n, "callback_data": f"reply:{n}"} for n in numbers[4:8]])

    return {"inline_keyboard": buttons}


def split_text(text: str, chunk_size: int) -> list[str]:
    """Split text into chunks, trying to break at newlines."""
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    current = ""

    for line in text.split("\n"):
        if len(line) > chunk_size:
            # Line itself exceeds chunk_size — flush current, then hard-split the line
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), chunk_size):
                chunks.append(line[i:i + chunk_size])
        elif len(current) + len(line) + 1 > chunk_size:
            if current:
                chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line

    if current:
        chunks.append(current)

    return chunks


@app.post("/notify/{event_type}")
async def notify(event_type: str, request: Request):
    """Called by Claude hooks to send notifications."""
    summary = None
    working_dir = None
    session_id = None
    try:
        data = await request.json()
        summary = data.get("summary")
        working_dir = data.get("working_dir")
        session_id = data.get("session_id")
    except Exception:
        pass

    logger.info(f"notify/{event_type}: working_dir={working_dir}, session_id={session_id}, has_summary={summary is not None}")

    # Always notify via dev bot — hook.py already skips bot-triggered sessions
    # (CLAUDE_TELEGRAM_BOT env check), so /notify only fires for external CLI
    # sessions (VS Code etc.) which should always go to the dev bot.
    target_bot = bots.get("dev")

    if not target_bot:
        return {"ok": False, "error": "No bot configured"}

    reply_markup = None

    if event_type == "completed":
        msg = "✅ <b>Claude has completed the task.</b>"
        if working_dir:
            dir_name = working_dir.split("/")[-1]
            msg = f"✅ <b>Claude has completed</b> (<code>{html.escape(dir_name)}</code>)"
        if summary:
            # Truncate to ~5 lines for preview
            lines = summary.split("\n")
            preview = "\n".join(lines[:5])
            if len(lines) > 5:
                preview += "\n…"
            # Cap at 800 chars
            if len(preview) > 800:
                preview = preview[:800] + "…"
            try:
                preview_html = markdown_to_telegram_html(preview)
            except Exception:
                preview_html = html.escape(preview)
            msg += f"\n\n{preview_html}"
        # Add "Continue" button if session_id is available
        if session_id:
            reply_markup = {"inline_keyboard": [[
                {"text": "Continue ➜", "callback_data": f"resume:{session_id}"},
            ]]}
            # Store working_dir for the resume callback (can't fit in callback_data)
            if working_dir:
                resume_working_dirs[session_id] = working_dir
    elif event_type == "waiting":
        msg = "⏸ Claude is waiting for input."
    else:
        msg = f"📢 Claude event: {event_type}"

    await telegram.send_message(
        msg, chat_id=target_bot.chat_id, parse_mode="HTML",
        api_url=target_bot.api_url, reply_markup=reply_markup,
    )
    return {"ok": True}


@app.post("/test")
async def test_message(request: Request):
    """Test endpoint - send a message as if from Telegram."""
    data = await request.json()
    text = data.get("text", "")

    dev_bot = bots.get("dev")
    if not dev_bot:
        return {"error": "No dev bot configured"}

    chat_id = str(dev_bot.chat_id)

    if not text:
        return {"error": "No text provided"}

    if text.startswith("/"):
        await handle_command(text, chat_id, dev_bot)
    else:
        runner = get_runner_for_bot(dev_bot)
        continue_session = runner.is_in_conversation()
        await run_claude(text, chat_id, dev_bot, continue_session=continue_session)

    return {"ok": True, "text": text}


def main():
    """Run the server."""
    import uvicorn
    uvicorn.run(
        "claude_telegram.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )


if __name__ == "__main__":
    main()
