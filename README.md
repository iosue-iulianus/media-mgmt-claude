# media-mgmt-claude

Ask Claude for movies and shows in plain language and have them land in
Radarr and Sonarr. It also answers "when does it come out" from their
calendars (falling back to a web search) and can report on or replace
downloads in the queue.

```
you:  @yourbot get the office
bot:  Which one?
      [The Office (US) (2005)]  [The Office (UK) (2001)]
you:  (taps the US one)
bot:  Which seasons?
      [✅ S1] [S2] [S3] [S4] [S5] … [All] [Get 1 season]
you:  (taps S2, S3, Get)
bot:  Added The Office (US) (2005), seasons 1 to 3. Searching now.
```

The core is a small MCP server (`server.py`) that talks to the Sonarr and
Radarr APIs. You can use it two ways:

- **Telegram bot**: a Docker container that hands each message to the Claude
  Code CLI with only these tools available. Good for a household or friends
  group chat.
- **Claude desktop app**: a local MCP server, so you can ask from the Claude
  app on your computer.

Both need Sonarr v4 and Radarr (API v3) reachable from where they run, and a
Claude subscription.

## Telegram bot

`bot/bot.py` runs each message through `claude -p` with only the media-mgmt
tools and WebSearch (for air and release dates the calendars don't have).
Other built-in tools (shell, files, web fetch) and slash commands are
disabled. Its instructions are in `bot/system-prompt.md`.

### Using it

- In a group, mention it: `@yourbot get Slow Horses`. It ignores everything
  else, including plain replies to its messages and commands for other bots.
  In a private chat, every message goes to it.
- Each person in each chat has their own conversation for follow-ups ("the
  2024 one"), which expires after `SESSION_IDLE_MINUTES` of inactivity.
  `/new@yourbot` starts a fresh one.
- Answers come as replies to the message that asked.
- When there is a choice (several matches, which download to cancel), the
  options come as buttons.
- Asking for a show with more than one season without naming seasons brings
  up a season picker: tap seasons to toggle them (the first is preselected),
  All/Clear, then Get. Named seasons, "the whole show", one-season shows, and
  unaired shows are added without asking.
- Only the person who asked can tap a choice or the season picker.

### Setup

It uses your Claude subscription through a long-lived token, so requests count
against your plan's usage limits. Subscription credentials are for your own
use, so run your own copy with your own token rather than sharing one.

1. Create a bot with @BotFather and note its token. To use it in a group, add
   it to the group; the bot filters messages itself, so privacy mode can be
   disabled (`/setprivacy`).
2. On any machine signed in to Claude Code, run `claude setup-token` and copy
   the token it prints (valid for one year).
3. Clone this repo onto the Docker host. Create `.env` as described under
   [Configuration](#configuration), then `cp bot.env.example bot.env` and fill
   it in:

   | Variable | What |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | From @BotFather |
   | `ALLOWED_TELEGRAM_CHATS` | Comma-separated chat IDs it answers in. Messages from other chats are ignored and their ID is logged, so leave it empty at first and see step 4. Group IDs are negative. |
   | `CLAUDE_CODE_OAUTH_TOKEN` | From `claude setup-token` |
   | `CLAUDE_MODEL` | `sonnet` (default) or `haiku`. Haiku is faster and uses much less of your plan; the tools and prompt are written so it can follow them. |
   | `SESSION_IDLE_MINUTES` | How long a conversation lasts between messages (default 30) |

4. `docker compose up -d --build`, then `docker compose logs -f`. Message the
   bot from the chat you want to use (in a group, mention it), add the ID
   from the "ignored message from unauthorized chat" log line to
   `ALLOWED_TELEGRAM_CHATS`, and `docker compose up -d --force-recreate`.

Changes to `bot.env` need `docker compose up -d --force-recreate`. Changes to
code or `system-prompt.md` need `docker compose up -d --build`, which also
clears open conversations and buttons.

Claude Code is installed from Anthropic's apt repository and does not
auto-update; rebuild the image (`docker compose build --pull`) to upgrade.

### Networking

The container only makes outgoing connections (Telegram, Anthropic, Sonarr,
Radarr), so Docker's default network works and nothing needs to be exposed.
The URLs in `.env` must resolve inside the container; if local hostnames
don't, use IP addresses. Don't use `localhost` for services on the Docker
host: inside the container that is the container itself.

To give the bot its own address on the LAN instead (for example on an
existing macvlan network), put the network in a
`docker-compose.override.yaml`, which Compose merges automatically and git
ignores:

```yaml
services:
  media-bot:
    networks:
      macvlan:
        ipv4_address: 192.168.1.60
        mac_address: 02:42:c0:a8:01:3c

networks:
  macvlan:
    external: true
```

A container on macvlan can't reach its own host's IP, so this only works if
Sonarr and Radarr are on a different address than the Docker host.

The compose file mounts the host's `/etc/localtime` so air times are in local
time.

### Logs

Each Claude run logs one line, for example:

```
run chat=-1001234567890 user=123456789 success turns=3 secs=6.9 cost=$0.0284 in=18 out=496 cache_read=10756 tools=search_movie,search_show
```

`docker compose logs | grep " run "` shows what each request did: how many
turns it took, how long, what it would cost at API rates, and which tools it
called. Errors are logged in full; the chat only gets a short "something
went wrong".

### NAS notes

- The compose file is named `docker-compose.yaml` because some NAS Docker
  UIs (UGREEN, for one) only detect `.yaml`.
- Some NAS Docker installs build without BuildKit, and files copied from a
  share can arrive without world-read permission, so the Dockerfile runs
  `chmod` after `COPY` instead of using `COPY --chmod`.

## Claude desktop app

The server runs as a local child process of the Claude app, so it can reach
Sonarr and Radarr on your LAN. Requires [uv](https://docs.astral.sh/uv/).

1. Create `.env` as described under [Configuration](#configuration).
2. Smoke test (it should start and wait silently on stdin; Ctrl-C to quit):
   ```
   uv run server.py
   ```
3. Add it to the Claude app's config file as a top-level key, using the
   absolute path from `which uv` (`where uv` on Windows). The file is
   `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS
   and `%APPDATA%\Claude\claude_desktop_config.json` on Windows.
   ```json
   "mcpServers": {
     "media-mgmt": {
       "command": "/absolute/path/to/uv",
       "args": ["run", "/absolute/path/to/media-mgmt-claude/server.py"]
     }
   }
   ```
4. Quit and reopen the Claude app. Restart it again after any change to
   `server.py`. On macOS, logs are in
   `~/Library/Logs/Claude/mcp-server-media-mgmt.log`.

On macOS, the first call after a fresh install can fail with "No route to
host" until Local Network access is allowed for the Claude app.

## Configuration

`cp .env.example .env` and fill in:

| Variable | What |
|---|---|
| `SONARR_URL`, `RADARR_URL` | Base URLs, e.g. `http://192.168.1.50:8989` |
| `SONARR_API_KEY`, `RADARR_API_KEY` | From each app's Settings > General |
| `SONARR_ROOT_FOLDER`, `RADARR_ROOT_FOLDER` | Default library folder; must match a root folder in the app exactly. Blank uses the first one. |
| `<SERVICE>_ROOT_FOLDER_<CATEGORY>` | Optional per-category folder, e.g. `RADARR_ROOT_FOLDER_ANIME` |
| `SONARR_QUALITY_PROFILE`, `RADARR_QUALITY_PROFILE` | Profile name; blank uses the first one |
| `<SERVICE>_QUALITY_PROFILE_<CATEGORY>` | Optional per-category profile |

Categories are `movie`, `anime`, and `standup` for Radarr, and `tv` and
`anime` for Sonarr (which also sets the anime series type). Claude picks the
category from the title's genre and language; a category without its own
folder or profile uses the default.

`.env` is read on every call, so config changes need no restart.

## Tools

| Tool | Does |
|---|---|
| `search_movie(title)` | Radarr lookup with TMDB id, release dates, and whether it's downloaded |
| `add_movie(tmdb_id, category)` | Adds monitored and searches; for a movie already in Radarr, monitors and searches |
| `search_show(title)` | Sonarr lookup with TVDB id and season numbers; library shows include air dates and per-season episode counts |
| `add_show(tvdb_id, seasons, category)` | Adds the series with all or the given seasons; for a show already in Sonarr, switches those seasons on |
| `queue_status()` | Current download queue for both services |
| `replace_download(service, download_id)` | Cancels a dead download, blocklists the release, and searches again |
| `show_schedule(tvdb_id)` | Last aired, next airing, and upcoming episodes of a show in Sonarr |
| `upcoming(days)` | Sonarr and Radarr calendars for the next few days (up to 90) |

It is add-only by design, with one exception: `replace_download` can cancel a
download in the queue so a different release gets grabbed. It cannot delete
movies, shows, or files, unmonitor, change settings, or reach hosts other than
the two in `.env`.

Search results are ranked before being cut to eight: titles already in the
library first, then exact title matches, then by vote count. Times are shown
in the local time zone of whatever runs the server.

## Notes

- Pinned to `mcp>=1.28,<2` (the v1 SDK line).
- The bot uses only the Python standard library; the server needs `mcp`.

## License

MIT. See [LICENSE](LICENSE).
