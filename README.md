# media-mgmt-claude

A small local MCP server that lets Claude request movies and shows from Radarr
and Sonarr. It runs as a stdio child process of the Claude desktop app, so it
can reach services on the LAN that a cloud session cannot.

## Tools

| Tool | Does |
|---|---|
| `search_movie(title)` | Radarr lookup, returns candidates with TMDB id |
| `add_movie(tmdb_id, category)` | Adds monitored and starts a search |
| `search_show(title)` | Sonarr lookup, returns candidates with TVDB id |
| `add_show(tvdb_id, seasons, category)` | Adds the series, monitoring all or the given seasons |
| `monitor_seasons(tvdb_id, seasons)` | Switches on more seasons of a show already in Sonarr |
| `queue_status()` | Current download queue for both services |

It is add-only by design: no delete, no unmonitor, no settings changes, and no
hosts other than the two in `.env`.

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

## Notes

- Pinned to `mcp>=1.28,<2` (the v1 SDK line).
- The first call after a fresh install can fail with "No route to host" until
  macOS Local Network access is allowed for the Claude app.
