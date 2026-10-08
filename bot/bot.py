"""Telegram front end for the media-mgmt MCP server.

Each Telegram message is handed to the Claude Code CLI (claude -p) with only
the media-mgmt tools available. Built-in tools (shell, files, web) and slash
commands are disabled, so a message can only ever search, add, or check the
queue. Each chat keeps one Claude session for follow-ups ("the 2024 one"),
which expires after a period of inactivity.

Uses only the standard library. Configuration comes from environment
variables, see bot.env.example.
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_IDS = {
    int(chat_id) for chat_id in os.environ["ALLOWED_TELEGRAM_CHATS"].split(",") if chat_id.strip()
}
if not ALLOWED_CHAT_IDS:
    raise SystemExit("ALLOWED_TELEGRAM_CHATS is empty; set at least one chat ID")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
SESSION_IDLE_SECONDS = int(os.environ.get("SESSION_IDLE_MINUTES", "30")) * 60

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
POLL_TIMEOUT_SECONDS = 50
CLAUDE_TIMEOUT_SECONDS = 180
MAX_TELEGRAM_MESSAGE = 4000
IGNORE_MESSAGES_OLDER_THAN_SECONDS = 300

MEDIA_TOOLS = [
    "mcp__media-mgmt__search_movie",
    "mcp__media-mgmt__add_movie",
    "mcp__media-mgmt__monitor_movie",
    "mcp__media-mgmt__search_show",
    "mcp__media-mgmt__add_show",
    "mcp__media-mgmt__monitor_seasons",
    "mcp__media-mgmt__queue_status",
]

HELP_TEXT = (
    "Ask in plain language, for example:\n"
    "  download Silver Linings Playbook\n"
    "  get season 1 of Slow Horses\n"
    "  what's downloading?\n\n"
    "/new starts a fresh conversation."
)

# chat_id -> (claude session id, unix time of last use)
sessions: dict[int, tuple[str, float]] = {}

# Filled in from getMe at startup. In group chats the bot only answers
# messages that @mention it or reply to it, and commands addressed to it.
BOT_ID = 0
BOT_USERNAME = ""


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def telegram(method: str, params: dict, http_timeout: float = 30):
    """Call a Telegram Bot API method and return its result."""
    request = urllib.request.Request(
        f"{TELEGRAM_API}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=http_timeout) as response:
        body = json.load(response)
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body.get('description')}")
    return body["result"]


def send_message(chat_id: int, text: str) -> None:
    """Send plain text, split into chunks that fit Telegram's message limit."""
    text = text.strip() or "(empty reply)"
    for start in range(0, len(text), MAX_TELEGRAM_MESSAGE):
        chunk = text[start : start + MAX_TELEGRAM_MESSAGE]
        telegram("sendMessage", {"chat_id": chat_id, "text": chunk})


def keep_typing(chat_id: int, stop: threading.Event) -> None:
    """Show the typing indicator until stop is set (Telegram clears it after ~5s)."""
    while not stop.is_set():
        try:
            telegram("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except (OSError, RuntimeError):
            pass
        stop.wait(4)


def claude_command(text: str, session_id: str | None) -> list[str]:
    # The prompt is one argv element that always starts with this fixed prefix,
    # so message content can never be parsed as a CLI flag.
    prompt = f"Telegram message:\n{text}"
    command = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--model", CLAUDE_MODEL,
        "--mcp-config", str(APP_DIR / "mcp.json"),
        "--strict-mcp-config",
        "--tools", "",
        "--allowedTools", ",".join(MEDIA_TOOLS),
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--disable-slash-commands",
        "--append-system-prompt-file", str(APP_DIR / "system-prompt.md"),
        "--max-turns", "10",
    ]
    if session_id:
        command += ["--resume", session_id]
    return command


def ask_claude(text: str, session_id: str | None) -> tuple[str, str | None]:
    """Run one claude -p turn. Returns (reply, session id to resume next time)."""
    completed = subprocess.run(
        claude_command(text, session_id),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT_SECONDS,
        cwd=APP_DIR,
    )
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError:
        detail = (completed.stderr or completed.stdout).strip()[-500:]
        raise RuntimeError(f"claude exited with code {completed.returncode}: {detail}")
    reply = output.get("result") or "(no reply)"
    if output.get("is_error"):
        return f"Claude reported an error: {reply}", None
    return reply, output.get("session_id")


def current_session(chat_id: int) -> str | None:
    entry = sessions.get(chat_id)
    if entry and time.time() - entry[1] < SESSION_IDLE_SECONDS:
        return entry[0]
    sessions.pop(chat_id, None)
    return None


def is_reply_to_bot(message: dict) -> bool:
    return message.get("reply_to_message", {}).get("from", {}).get("id") == BOT_ID


def handle_message(message: dict) -> None:
    chat_id = message["chat"]["id"]
    if chat_id not in ALLOWED_CHAT_IDS:
        log(f"ignored message from unauthorized chat {chat_id}")
        return
    if time.time() - message.get("date", 0) > IGNORE_MESSAGES_OLDER_THAN_SECONDS:
        log(f"ignored stale message {message.get('message_id')}")
        return

    private = message["chat"]["type"] == "private"
    text = (message.get("text") or "").strip()
    mention = re.compile(rf"@{re.escape(BOT_USERNAME)}\b", re.IGNORECASE)

    if text.startswith("/"):
        command, _, target = text.split()[0].lower().partition("@")
        # In a group, bare commands and /cmd@otherbot belong to other bots.
        if not private and target != BOT_USERNAME.lower():
            return
        if command in ("/start", "/help"):
            send_message(chat_id, HELP_TEXT)
        elif command == "/new":
            sessions.pop(chat_id, None)
            send_message(chat_id, "Started a fresh conversation.")
        else:
            send_message(chat_id, "Unknown command. " + HELP_TEXT)
        return

    if not (private or mention.search(text) or is_reply_to_bot(message)):
        return
    if not text:
        send_message(chat_id, "Only text messages are supported for now.")
        return
    text = mention.sub("", text).strip()
    if not text:
        send_message(chat_id, HELP_TEXT)
        return

    stop_typing = threading.Event()
    threading.Thread(target=keep_typing, args=(chat_id, stop_typing), daemon=True).start()
    try:
        reply, session_id = ask_claude(text, current_session(chat_id))
    except subprocess.TimeoutExpired:
        reply, session_id = "Claude took too long to answer. Try again.", None
    except (OSError, RuntimeError) as error:
        log(f"claude failed: {error}")
        reply, session_id = f"Something went wrong: {error}", None
    finally:
        stop_typing.set()

    if session_id:
        sessions[chat_id] = (session_id, time.time())
    send_message(chat_id, reply)


def poll_updates(offset: int | None) -> list[dict]:
    """Long-poll Telegram for new messages."""
    params = {"timeout": POLL_TIMEOUT_SECONDS, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    return telegram("getUpdates", params, http_timeout=POLL_TIMEOUT_SECONDS + 10)


def main() -> None:
    global BOT_ID, BOT_USERNAME
    me = telegram("getMe", {})
    BOT_ID, BOT_USERNAME = me["id"], me["username"]
    log(
        f"starting as @{BOT_USERNAME}, model={CLAUDE_MODEL}, "
        f"allowed chats={sorted(ALLOWED_CHAT_IDS)}"
    )
    offset = None
    while True:
        try:
            updates = poll_updates(offset)
        except (OSError, RuntimeError) as error:
            log(f"polling failed: {error}")
            time.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            if "message" in update:
                try:
                    handle_message(update["message"])
                except Exception as error:  # keep the bot alive on any single bad message
                    log(f"error handling update {update['update_id']}: {error!r}")


if __name__ == "__main__":
    main()
