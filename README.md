# media-mgmt-claude

A small MCP server that lets Claude request movies and shows from Radarr and
Sonarr, answer "when does it come out" from their calendars, and report or
fix the download queue. It runs two ways:

- **Claude desktop app**: as a stdio child process on your Mac, so it can
  reach services on the LAN that a cloud session cannot.
- **Telegram bot**: in a Docker container (on a UGREEN NAS here) that hands
  each message to the Claude Code CLI with only these tools available.

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

Categories map to root folders via `.env`: `movie`, `anime`, `standup` for
Radarr, and `tv`, `anime` for Sonarr (which also sets the anime series type).

## Configuration

Requires Sonarr v4 and Radarr with API v3. `cp .env.example .env` and fill in:

| Variable | What |
|---|---|
| `SONARR_URL`, `RADARR_URL` | Base URLs, e.g. `http://media-mgmt.lan:8989` |
| `SONARR_API_KEY`, `RADARR_API_KEY` | From each app's Settings > General |
| `SONARR_ROOT_FOLDER`, `RADARR_ROOT_FOLDER` | Default library folder; must match a root folder in the app exactly |
| `<SERVICE>_ROOT_FOLDER_<CATEGORY>` | Per-category folder, e.g. `RADARR_ROOT_FOLDER_ANIME` |
| `SONARR_QUALITY_PROFILE`, `RADARR_QUALITY_PROFILE` | Profile name; blank uses the first one |
| `<SERVICE>_QUALITY_PROFILE_<CATEGORY>` | Optional per-category profile |

`.env` is read on every call, so config changes need no restart.

## Claude desktop app

Requires [uv](https://docs.astral.sh/uv/).

1. Create `.env` as above.
2. Smoke test (it should start and wait silently on stdin; Ctrl-C to quit):
   ```
   uv run server.py
   ```
3. Register it in `~/Library/Application Support/Claude/claude_desktop_config.json`
   as a top-level key, using the absolute path from `which uv`:
   ```json
   "mcpServers": {
     "media-mgmt": {
       "command": "/opt/homebrew/bin/uv",
       "args": ["run", "/absolute/path/to/media-mgmt-claude/server.py"]
     }
   }
   ```
4. Quit and reopen the Claude desktop app. Restart it again after any change
   to `server.py`. Logs are in `~/Library/Logs/Claude/mcp-server-media-mgmt.log`.

The first call after a fresh install can fail with "No route to host" until
macOS Local Network access is allowed for the Claude app.

## Telegram bot

`bot/bot.py` runs each message through `claude -p` with only the media-mgmt
tools and WebSearch (for air and release dates the calendars don't have).
Other built-in tools (shell, files, web fetch) and slash commands are
disabled. Its instructions are in `bot/system-prompt.md`, passed as a system
prompt rather than a `CLAUDE.md` so they carry system-level weight and don't
leak into coding sessions on this repo.

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
against your plan's usage limits. Keep it to your own use: subscription
credentials can't be used on other people's behalf.

1. Create a bot with @BotFather and note its token. To use it in a group, add
   it to the group; the bot filters messages itself, so privacy mode can be
   disabled (`/setprivacy`).
2. On any machine signed in to Claude Code, run `claude setup-token` and copy
   the token it prints (valid for one year).
3. Next to `docker-compose.yaml`, put the same `.env` the MCP server uses, and
   `cp bot.env.example bot.env` and fill it in:

   | Variable | What |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | From @BotFather |
   | `ALLOWED_TELEGRAM_CHATS` | Comma-separated chat IDs it answers in (required). Messages from other chats are ignored and their ID is logged, so message the bot and copy the ID from the logs. Group IDs are negative. |
   | `CLAUDE_CODE_OAUTH_TOKEN` | From `claude setup-token` |
   | `CLAUDE_MODEL` | `sonnet` (default) or `haiku`. Haiku is faster and uses much less of your plan; the tools and prompt are written so it can follow them. |
   | `SESSION_IDLE_MINUTES` | How long a conversation lasts between messages (default 30) |

4. `docker compose up -d --build`, then `docker compose logs -f`.

Changes to `bot.env` need `docker compose up -d --force-recreate`. Changes to
code or `system-prompt.md` need `docker compose up -d --build`, which also
clears open conversations and buttons.

Claude Code is installed from Anthropic's apt repository and does not
auto-update; rebuild the image (`docker compose build --pull`) to upgrade.

### Networking

`docker-compose.yaml` attaches the container to an existing external Docker
network called `macvlan` with a fixed IP and MAC address, so it appears on the
LAN as its own host. Change `ipv4_address` and `mac_address` to a free
address on your network (check what's taken with
`docker network inspect macvlan`), or remove the `networks` sections to use
Docker's default network.

On macvlan the container cannot reach its own host's IP, so Sonarr and Radarr
must be on a different address than the Docker host. The URLs in `.env` must
resolve inside the container; if `.lan` names don't, use IP addresses.

The compose file mounts the host's `/etc/localtime` so air times are in local
time.

### Logs

Each Claude run logs one line, for example:

```
run chat=-4292… user=1402… success turns=3 secs=6.9 cost=$0.0284 in=18 out=496 cache_read=10756 tools=search_movie,search_show
```

`docker compose logs | grep " run "` shows what each request did: how many
turns it took, how long, what it cost at API rates, and which tools it
called. Errors are logged in full; the chat only gets a short "something
went wrong".

### Deploying to a UGREEN NAS

- Copy files with rsync to the shared-folder path, not the volume path.
  SFTP, scp, and rsync on UGREEN see shared folders at the root, while an SSH
  shell sees `/volume2/...`:
  ```
  rsync -av --exclude .git --exclude __pycache__ ./ user@nas:/docker/media-mgmt-claude/
  ```
  Run it from the repo root. `bot.env` is not in the repo, so rsync leaves the
  one on the NAS alone.
- The compose file is named `docker-compose.yaml` because the UGREEN Docker
  UI only detects `.yaml`.
- UGREEN's Docker builds without BuildKit, and files from the share arrive
  without world-read permission, so the Dockerfile runs `chmod` after `COPY`
  (it can't use `COPY --chmod`).

## Notes

- Pinned to `mcp>=1.28,<2` (the v1 SDK line).
- The bot uses only the Python standard library; the server needs `mcp`.
