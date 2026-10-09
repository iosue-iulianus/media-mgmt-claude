"""Telegram front end for the media-mgmt MCP server.

Each Telegram message is handed to the Claude Code CLI (claude -p) with only
the media-mgmt tools and web search available. Other built-in tools (shell,
files, web fetch) and slash commands are disabled, so a message can only ever
search, add, check the queue and calendars, replace a dead download, or look
something up on the web.

Each person in each chat keeps their own Claude session for follow-ups ("the
2024 one"), which expires after a period of inactivity. Conversations run in
parallel, one worker thread each, so a slow request doesn't hold up others.
When Claude offers choices, they are shown as buttons.

Uses only the standard library. Configuration comes from environment
variables, see bot.env.example.
"""

import json
import os
import queue
import re
import secrets
import subprocess
import threading
import time
import urllib.request
from functools import partial
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# Empty answers nobody, but still logs the ID of each chat that messages the bot.
ALLOWED_CHAT_IDS = {
    int(chat_id) for chat_id in os.environ.get("ALLOWED_TELEGRAM_CHATS", "").split(",") if chat_id.strip()
}
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "sonnet")
SESSION_IDLE_SECONDS = int(os.environ.get("SESSION_IDLE_MINUTES", "30")) * 60

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
POLL_TIMEOUT_SECONDS = 50
CLAUDE_TIMEOUT_SECONDS = 180
MAX_CONCURRENT_CLAUDE_RUNS = 3
MAX_TELEGRAM_MESSAGE = 4000
MAX_CHOICES = 8
IGNORE_MESSAGES_OLDER_THAN_SECONDS = 300

MEDIA_TOOLS = [
    "mcp__media-mgmt__search_movie",
    "mcp__media-mgmt__add_movie",
    "mcp__media-mgmt__search_show",
    "mcp__media-mgmt__add_show",
    "mcp__media-mgmt__queue_status",
    "mcp__media-mgmt__replace_download",
    "mcp__media-mgmt__show_schedule",
    "mcp__media-mgmt__upcoming",
]
# The only built-in tool, for release and air-date questions the Sonarr and
# Radarr calendars can't answer.
BUILTIN_TOOLS = ["WebSearch"]

HELP_TEXT = (
    "Ask in plain language, for example:\n"
    "  download Silver Linings Playbook\n"
    "  get season 1 of Slow Horses\n"
    "  what's downloading?\n\n"
    "/new starts a fresh conversation."
)

# Claude marks each option it offers with a line like
# "[choice] The Office (2005, NBC) | tvdb:73244"; those become buttons.
CHOICE_LINE = re.compile(r"^\[choice\]\s*(.+?)\s*\|\s*(.+?)\s*$")
# A season picker: "[seasons] The Office (US) (2005) | tvdb:73244 | 1,2,3".
# The bot toggles seasons itself and only sends Claude the final pick.
SEASONS_LINE = re.compile(r"^\[seasons\]\s*(.+?)\s*\|\s*(tvdb:\d+)\s*\|\s*([\d,\s]+?)\s*$")
SEASONS_PER_ROW = 5
MAX_SEASON_BUTTONS = 90  # Telegram allows 100 buttons per keyboard

# (chat_id, user_id) -> (claude session id, unix time of last use)
sessions: dict[tuple[int, int], tuple[str, float]] = {}

# Button token -> the option it stands for and who may pick it, see send_reply.
# Written by conversation workers and read by the polling thread.
choices: dict[str, dict] = {}
choices_lock = threading.Lock()

# (chat_id, user_id) -> that conversation's job queue, drained by one worker
# thread so its messages are answered in order.
conversations: dict[tuple[int, int], queue.Queue] = {}
conversations_lock = threading.Lock()
claude_slots = threading.Semaphore(MAX_CONCURRENT_CLAUDE_RUNS)

# Filled in from getMe at startup. In group chats the bot only answers
# messages that @mention it and commands addressed to it. Replies to the bot
# without a mention are ignored, so others in the group can't chat with it.
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


def send_message(
    chat_id: int, text: str, reply_to: int | None = None, keyboard: list | None = None
) -> None:
    """Send plain text, split into chunks that fit Telegram's message limit.

    The first chunk replies to reply_to, and the last one carries the buttons.
    """
    text = text.strip() or "(empty reply)"
    chunks = [
        text[start : start + MAX_TELEGRAM_MESSAGE]
        for start in range(0, len(text), MAX_TELEGRAM_MESSAGE)
    ]
    for index, chunk in enumerate(chunks):
        params = {"chat_id": chat_id, "text": chunk}
        if reply_to and index == 0:
            params["reply_parameters"] = {
                "message_id": reply_to,
                "allow_sending_without_reply": True,
            }
        if keyboard and index == len(chunks) - 1:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        telegram("sendMessage", params)


def send_reply(chat_id: int, user_id: int, reply_to: int, reply: str, request: str) -> None:
    """Send Claude's reply, turning [choice] lines into buttons and a
    [seasons] line into a season picker."""
    lines, options, picker = [], [], None
    for line in reply.splitlines():
        if match := CHOICE_LINE.match(line.strip()):
            options.append(match.groups())
        elif match := SEASONS_LINE.match(line.strip()):
            picker = picker or match.groups()
        else:
            lines.append(line)
    # Removing choice lines can leave a run of blank lines behind.
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() or "Which one?"

    with choices_lock:
        cutoff = time.time() - SESSION_IDLE_SECONDS
        for token in [token for token, choice in choices.items() if choice["created"] < cutoff]:
            del choices[token]
        if picker:
            keyboard = new_season_picker(chat_id, user_id, request, *picker)
        else:
            keyboard = new_choices(chat_id, user_id, request, options[:MAX_CHOICES])
    send_message(chat_id, text, reply_to, keyboard or None)


def new_choices(chat_id: int, user_id: int, request: str, options: list) -> list:
    """Store single-pick options and return one button row per option."""
    keyboard, tokens = [], []
    for label, payload in options:
        token = secrets.token_urlsafe(12)
        tokens.append(token)
        choices[token] = {
            "kind": "choice",
            "chat_id": chat_id,
            "user_id": user_id,
            "label": label,
            "payload": payload,
            "request": request,
            "siblings": tokens,  # shared list: picking one retires the whole set
            "created": time.time(),
        }
        keyboard.append([{"text": label, "callback_data": token}])
    return keyboard


def new_season_picker(
    chat_id: int, user_id: int, request: str, label: str, payload: str, numbers: str
) -> list | None:
    """Store a season picker, first season preselected, and return its keyboard."""
    seasons = sorted({int(number) for number in numbers.split(",") if number.strip()})
    if not seasons:
        return None
    token = secrets.token_urlsafe(12)
    choices[token] = {
        "kind": "seasons",
        "chat_id": chat_id,
        "user_id": user_id,
        "label": label,
        "payload": payload,
        "request": request,
        "seasons": seasons[:MAX_SEASON_BUTTONS],
        "selected": {seasons[0]},
        "siblings": [token],
        "created": time.time(),
    }
    return season_keyboard(token, choices[token])


def season_keyboard(token: str, picker: dict) -> list:
    """Season toggles, SEASONS_PER_ROW to a row, then All/Clear and Get."""
    selected = picker["selected"]
    toggles = [
        {
            "text": f"✅ S{number}" if number in selected else f"S{number}",
            "callback_data": f"{token}:{number}",
        }
        for number in picker["seasons"]
    ]
    rows = [
        toggles[start : start + SEASONS_PER_ROW]
        for start in range(0, len(toggles), SEASONS_PER_ROW)
    ]
    everything = selected == set(picker["seasons"])
    count = len(selected)
    get_label = f"Get {count} season{'' if count == 1 else 's'}"
    rows.append(
        [
            {"text": "Clear" if everything else "All", "callback_data": f"{token}:all"},
            {"text": get_label, "callback_data": f"{token}:get"},
        ]
    )
    return rows


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
        "--output-format", "stream-json",
        "--verbose",  # required by stream-json; it only adds events to stdout
        "--model", CLAUDE_MODEL,
        "--mcp-config", str(APP_DIR / "mcp.json"),
        "--strict-mcp-config",
        "--tools", ",".join(BUILTIN_TOOLS),
        "--allowedTools", ",".join(MEDIA_TOOLS + BUILTIN_TOOLS),
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--disable-slash-commands",
        "--append-system-prompt-file", str(APP_DIR / "system-prompt.md"),
        "--max-turns", "10",
    ]
    if session_id:
        command += ["--resume", session_id]
    return command


def ask_claude(text: str, session_id: str | None, key: tuple[int, int]) -> tuple[str, str | None]:
    """Run one claude -p turn. Returns (reply, session id to resume next time).

    Failures raise RuntimeError with the detail, which is logged but never
    sent to the chat.
    """
    completed = subprocess.run(
        claude_command(text, session_id),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT_SECONDS,
        cwd=APP_DIR,
    )
    events = []
    for line in completed.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    result = next((event for event in reversed(events) if event.get("type") == "result"), None)
    if result is None:
        detail = (completed.stderr or completed.stdout).strip()[-500:]
        raise RuntimeError(f"claude exited with code {completed.returncode}: {detail}")

    log_run(key, result, tools_called(events))
    if result.get("is_error"):
        raise RuntimeError(f"claude reported {result.get('subtype')}: {result.get('result')}")
    return result.get("result") or "(no reply)", result.get("session_id")


def tools_called(events: list[dict]) -> list[str]:
    names = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                names.append(block["name"].removeprefix("mcp__media-mgmt__"))
    return names


def log_run(key: tuple[int, int], result: dict, tools: list[str]) -> None:
    """One line per Claude run, to see how the model is doing without guessing."""
    usage = result.get("usage") or {}
    denied = [denial.get("tool_name") for denial in result.get("permission_denials") or []]
    log(
        f"run chat={key[0]} user={key[1]} {result.get('subtype')} "
        f"turns={result.get('num_turns')} secs={(result.get('duration_ms') or 0) / 1000:.1f} "
        f"cost=${result.get('total_cost_usd') or 0:.4f} "
        f"in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
        f"cache_read={usage.get('cache_read_input_tokens')} "
        f"tools={','.join(tools) or '-'}" + (f" denied={','.join(denied)}" if denied else "")
    )


def current_session(key: tuple[int, int]) -> str | None:
    entry = sessions.get(key)
    if entry and time.time() - entry[1] < SESSION_IDLE_SECONDS:
        return entry[0]
    sessions.pop(key, None)
    return None


def answer(chat_id: int, user_id: int, reply_to: int, text: str) -> None:
    """Run text through Claude in this person's session and send the reply."""
    key = (chat_id, user_id)
    stop_typing = threading.Event()
    threading.Thread(target=keep_typing, args=(chat_id, stop_typing), daemon=True).start()
    try:
        with claude_slots:
            reply, session_id = ask_claude(text, current_session(key), key)
    except subprocess.TimeoutExpired:
        log(f"claude timed out for chat={chat_id} user={user_id}")
        reply, session_id = "That took too long. Try again.", None
    except (OSError, RuntimeError) as error:
        log(f"claude failed for chat={chat_id} user={user_id}: {error}")
        reply, session_id = "Something went wrong on my end. Try again in a bit.", None
    finally:
        stop_typing.set()

    if session_id:
        sessions[key] = (session_id, time.time())
    send_reply(chat_id, user_id, reply_to, reply, text)


def enqueue(key: tuple[int, int], job) -> None:
    """Queue a job for this conversation, starting its worker on first use."""
    with conversations_lock:
        jobs = conversations.get(key)
        if jobs is None:
            jobs = conversations[key] = queue.Queue()
            threading.Thread(target=conversation_worker, args=(key, jobs), daemon=True).start()
    jobs.put(job)


def conversation_worker(key: tuple[int, int], jobs: queue.Queue) -> None:
    while True:
        job = jobs.get()
        try:
            job()
        except Exception as error:  # keep the worker alive on any single bad job
            log(f"error in conversation {key}: {error!r}")


def handle_message(message: dict) -> None:
    chat_id = message["chat"]["id"]
    if chat_id not in ALLOWED_CHAT_IDS:
        log(f"ignored message from unauthorized chat {chat_id}")
        return
    if time.time() - message.get("date", 0) > IGNORE_MESSAGES_OLDER_THAN_SECONDS:
        log(f"ignored stale message {message.get('message_id')}")
        return

    user_id = message.get("from", {}).get("id", chat_id)
    key = (chat_id, user_id)
    message_id = message["message_id"]
    private = message["chat"]["type"] == "private"
    text = (message.get("text") or "").strip()
    mention = re.compile(rf"@{re.escape(BOT_USERNAME)}\b", re.IGNORECASE)

    if text.startswith("/"):
        command, _, target = text.split()[0].lower().partition("@")
        # In a group, bare commands and /cmd@otherbot belong to other bots.
        if not private and target != BOT_USERNAME.lower():
            return
        if command in ("/start", "/help"):
            send_message(chat_id, HELP_TEXT, message_id)
        elif command == "/new":
            sessions.pop(key, None)
            send_message(chat_id, "Started a fresh conversation.", message_id)
        else:
            send_message(chat_id, "Unknown command. " + HELP_TEXT, message_id)
        return

    if not (private or mention.search(text)):
        return
    if not text:
        send_message(chat_id, "Only text messages are supported for now.", message_id)
        return
    text = mention.sub("", text).strip()
    if not text:
        send_message(chat_id, HELP_TEXT, message_id)
        return
    enqueue(key, partial(answer, chat_id, user_id, message_id, text))


def handle_button(callback: dict) -> None:
    """A tap on a [choice] button or a season picker button from send_reply.

    Choice buttons carry just a token. Season picker buttons carry
    "<token>:<season number>", "<token>:all", or "<token>:get"; only "get"
    goes to Claude, the toggles are handled here.
    """
    message = callback.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    user_id = callback["from"]["id"]
    token, _, action = callback.get("data", "").partition(":")
    choice = choices.get(token)

    def acknowledge(notice: str | None = None) -> None:
        params = {"callback_query_id": callback["id"]}
        if notice:
            params["text"] = notice
        telegram("answerCallbackQuery", params)

    if chat_id not in ALLOWED_CHAT_IDS or not choice or choice["chat_id"] != chat_id:
        acknowledge("That choice has expired. Ask again.")
        return
    if choice["user_id"] != user_id:
        acknowledge("Only the person who asked can pick.")
        return

    if choice["kind"] == "seasons":
        if action != "get":
            toggle_seasons(choice, action)
            acknowledge()
            telegram(
                "editMessageReplyMarkup",
                {
                    "chat_id": chat_id,
                    "message_id": message["message_id"],
                    "reply_markup": {"inline_keyboard": season_keyboard(token, choice)},
                },
            )
            return
        if not choice["selected"]:
            acknowledge("Pick at least one season.")
            return
        numbers = ", ".join(str(number) for number in sorted(choice["selected"]))
        picked = f"{choice['label']}, seasons {numbers}"
        payload = f"{choice['payload']}; seasons:{numbers.replace(' ', '')}"
    else:
        picked, payload = choice["label"], choice["payload"]
    acknowledge()

    with choices_lock:
        for sibling in choice["siblings"]:
            choices.pop(sibling, None)
    try:
        # Editing the text without reply_markup also removes the buttons.
        telegram(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message["message_id"],
                "text": f"{message.get('text', '')}\n\nPicked: {picked}",
            },
        )
    except (OSError, RuntimeError) as error:
        log(f"could not mark choice as picked: {error}")

    # Repeat the original request so the pick still makes sense if the
    # session expired in the meantime.
    text = f"{choice['request']}\nPicked: {picked} [{payload}]"
    enqueue((chat_id, user_id), partial(answer, chat_id, user_id, message["message_id"], text))


def toggle_seasons(picker: dict, action: str) -> None:
    """Apply one season picker tap: a season number, or "all" to select or clear all."""
    with choices_lock:
        if action == "all":
            everything = set(picker["seasons"])
            picker["selected"] = set() if picker["selected"] == everything else everything
        elif action.isdigit() and int(action) in picker["seasons"]:
            picker["selected"] ^= {int(action)}


def poll_updates(offset: int | None) -> list[dict]:
    """Long-poll Telegram for new messages and button taps."""
    params = {"timeout": POLL_TIMEOUT_SECONDS, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        params["offset"] = offset
    return telegram("getUpdates", params, http_timeout=POLL_TIMEOUT_SECONDS + 10)


def main() -> None:
    global BOT_USERNAME
    BOT_USERNAME = telegram("getMe", {})["username"]
    log(
        f"starting as @{BOT_USERNAME}, model={CLAUDE_MODEL}, "
        f"allowed chats={sorted(ALLOWED_CHAT_IDS)}"
    )
    if not ALLOWED_CHAT_IDS:
        log("ALLOWED_TELEGRAM_CHATS is empty: answering nobody. Message the bot and copy the chat ID below.")
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
            try:
                if "message" in update:
                    handle_message(update["message"])
                elif "callback_query" in update:
                    handle_button(update["callback_query"])
            except Exception as error:  # keep the bot alive on any single bad update
                log(f"error handling update {update['update_id']}: {error!r}")


if __name__ == "__main__":
    main()
