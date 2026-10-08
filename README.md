# media-mgmt-claude

A small local MCP server that lets Claude request movies and shows from Radarr
and Sonarr. It runs as a stdio child process of the Claude desktop app, so it
can reach services on the LAN that a cloud session cannot.

## Tools

| Tool | Does |
|---|---|
| `search_movie(title)` | Radarr lookup with TMDB id, release dates, and whether it's downloaded |
| `add_movie(tmdb_id, category)` | Adds monitored and searches; for a movie already in Radarr, monitors and searches |
| `search_show(title)` | Sonarr lookup with TVDB id; library shows include air dates and per-season episode counts |
| `add_show(tvdb_id, seasons, category)` | Adds the series with all or the given seasons; for a show already in Sonarr, switches those seasons on |
| `queue_status()` | Current download queue for both services |
| `show_schedule(tvdb_id)` | Last aired, next airing, and upcoming episodes of a show in Sonarr |
| `upcoming(days)` | Sonarr and Radarr calendars for the next few days |
| `replace_download(service, download_id)` | Cancels a dead download, blocklists the release, and searches again |

It is add-only by design, with one exception: `replace_download` can cancel a
download in the queue so a different release gets grabbed. It cannot delete
movies, shows, or files, unmonitor, change settings, or reach hosts other than
the two in `.env`.

Categories map to root folders via `.env`: `movie`, `anime`, `standup` for
Radarr, and `tv`, `anime` for Sonarr (which also sets the anime series type).

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Sonarr v4 / Radarr with API v3.

1. `cp .env.example .env` and fill in the API keys, URLs, and folders.
2. Smoke test (it should start and wait silently on stdin):
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
4. Quit and reopen the Claude desktop app. Logs are in
   `~/Library/Logs/Claude/mcp-server-media-mgmt.log`.

`.env` is read on every call, so config changes need no restart. Changes to
`server.py` do.

## Telegram bot (Docker)

`bot/bot.py` is a Telegram front end that hands each message to the Claude
Code CLI (`claude -p`) with only the media-mgmt tools and WebSearch (for air
and release dates the calendars don't have) available. Other built-in tools
(shell, files, web fetch) and slash commands are disabled, and only the
Telegram chats in `ALLOWED_TELEGRAM_CHATS` are answered. Each person in each
chat keeps their own Claude session for follow-ups; `/new` starts a fresh
one. In a group it only answers messages that @mention it, including
follow-ups, and commands addressed as `/new@botname`, so it can share a group
with other bots and people.

Replies are sent as replies to the message that asked. When there is a
choice to make (which match, which download) the options come as buttons.
Asking for a multi-season show without naming seasons brings up a season
picker: toggle seasons on and off (the first is preselected), then tap Get.
Only the person who asked can pick. Conversations run in
parallel, at most three Claude runs at a time. Each run logs one `run` line
with its turns, time, cost, tokens, and the tools it called; errors go to the
log and the chat just gets a short "something went wrong".

It uses your Claude subscription through a long-lived token, so requests count
against your plan's usage limits. Keep it to your own use: subscription
credentials can't be used on other people's behalf.

1. Create a bot with @BotFather and note its token.
2. On any machine signed in to Claude Code, run `claude setup-token` and copy
   the token it prints (valid for one year).
3. On the Docker host, next to `docker-compose.yaml`:
   - `cp bot.env.example bot.env` and fill it in
   - put the same `.env` the MCP server uses alongside it
4. `docker compose up -d --build`, then `docker compose logs -f`.

The container must be able to resolve and reach the Sonarr and Radarr URLs in
`.env`. If `.lan` names don't resolve inside Docker, use IP addresses there.

Claude Code is installed from Anthropic's apt repository and does not
auto-update; rebuild the image (`docker compose build --pull`) to upgrade.

## Notes

- Pinned to `mcp>=1.28,<2` (the v1 SDK line).
- The first call after a fresh install can fail with "No route to host" until
  macOS Local Network access is allowed for the Claude app.
