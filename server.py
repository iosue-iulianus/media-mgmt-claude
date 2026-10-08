# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.28,<2"]
# ///
"""MCP server exposing a small, fixed set of Sonarr and Radarr operations.

Runs as a stdio child process of the Claude desktop app. It reads connection
details from the .env file next to this script, so API keys never leave this
machine. It can search, add, switch on monitoring for a movie or for more
seasons of a show, and report the download queue. It cannot delete, unmonitor, change settings, or
reach any host other than the two configured in .env.

Run manually for a smoke test:  uv run server.py
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

ENV_FILE = Path(__file__).resolve().parent / ".env"
TIMEOUT_SECONDS = 20
MAX_SEARCH_RESULTS = 8
QUEUE_PAGE_SIZE = 1000

mcp = FastMCP("media-mgmt")


class ArrError(Exception):
    """A problem talking to Sonarr or Radarr, with a message safe to show."""


def load_config() -> dict[str, str]:
    """Parse KEY=VALUE lines from .env. Read on every call so edits apply at once."""
    config = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip().strip("'\"")
    return config


def api(service: str, method: str, path: str, params: dict | None = None, body: dict | None = None):
    """Call one /api/v3 endpoint on SONARR or RADARR and return the parsed JSON."""
    config = load_config()
    base_url = config.get(f"{service}_URL", "").rstrip("/")
    api_key = config.get(f"{service}_API_KEY", "")
    if not base_url or not api_key:
        raise ArrError(f"{service}_URL or {service}_API_KEY is not set in {ENV_FILE.name}")

    url = f"{base_url}/api/v3/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        raise ArrError(f"{service} returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise ArrError(f"Could not reach {service} at {base_url}: {error.reason}") from error


def category_setting(service: str, setting: str, category: str) -> str | None:
    """Look up a per-category .env value, falling back to the service default.

    For example ("RADARR", "ROOT_FOLDER", "standup") reads
    RADARR_ROOT_FOLDER_STANDUP, then RADARR_ROOT_FOLDER.
    """
    config = load_config()
    specific = config.get(f"{service}_{setting}_{category.upper()}")
    return specific or config.get(f"{service}_{setting}") or None


def root_folder(service: str, category: str) -> str:
    """Root folder for this category from .env. Must exist in the service."""
    available = [folder["path"] for folder in api(service, "GET", "rootfolder")]
    if not available:
        raise ArrError(f"{service} has no root folders configured")
    configured = category_setting(service, "ROOT_FOLDER", category)
    if not configured:
        return available[0]
    if configured not in available:
        raise ArrError(
            f"Root folder '{configured}' from {ENV_FILE.name} is not configured in "
            f"{service}. Available: {', '.join(available)}"
        )
    return configured


def quality_profile_id(service: str, category: str) -> int:
    """Quality profile for this category named in .env, or the service's first."""
    profiles = api(service, "GET", "qualityprofile")
    if not profiles:
        raise ArrError(f"{service} has no quality profiles configured")
    wanted = category_setting(service, "QUALITY_PROFILE", category)
    if not wanted:
        return profiles[0]["id"]
    for profile in profiles:
        if profile["name"].lower() == wanted.lower():
            return profile["id"]
    names = ", ".join(profile["name"] for profile in profiles)
    raise ArrError(f"{service} quality profile '{wanted}' not found. Available: {names}")


@mcp.tool()
def search_movie(title: str) -> list[dict]:
    """Search Radarr for movies matching a title. Returns candidates with tmdb_id.

    Call this before add_movie. If more than one plausible match comes back
    (remakes, same title in different years), ask the user which one they mean.
    """
    results = api("RADARR", "GET", "movie/lookup", params={"term": title})
    return [
        {
            "title": movie.get("title"),
            "year": movie.get("year"),
            "tmdb_id": movie.get("tmdbId"),
            "status": movie.get("status"),
            "genres": movie.get("genres"),
            "original_language": (movie.get("originalLanguage") or {}).get("name"),
            "in_library": bool(movie.get("id")),
            "overview": (movie.get("overview") or "")[:200],
        }
        for movie in results[:MAX_SEARCH_RESULTS]
    ]


@mcp.tool()
def add_movie(tmdb_id: int, category: Literal["movie", "anime", "standup"] = "movie") -> str:
    """Add a movie to Radarr by TMDB id, monitored, and start a search for it.

    category picks the library folder: "anime" for Japanese animated films,
    "standup" for stand-up comedy specials, "movie" for everything else. Use
    the genres and original_language from search_movie to decide, and ask the
    user if it is unclear. Unreleased movies are added and grabbed once available.
    """
    movie = api("RADARR", "GET", "movie/lookup/tmdb", params={"tmdbId": tmdb_id})
    label = f"{movie.get('title')} ({movie.get('year')})"
    if movie.get("id"):
        return f"{label} is already in Radarr. Use monitor_movie to monitor and search for it."

    folder = root_folder("RADARR", category)
    movie.update(
        {
            "qualityProfileId": quality_profile_id("RADARR", category),
            "rootFolderPath": folder,
            "monitored": True,
            "addOptions": {"searchForMovie": True},
        }
    )
    api("RADARR", "POST", "movie", body=movie)
    return f"Added {label} to Radarr in {folder} and started a search."


@mcp.tool()
def monitor_movie(tmdb_id: int) -> str:
    """Monitor a movie already in Radarr and search for it.

    Use this when add_movie reports the movie is already in Radarr but it was
    never downloaded, for example because it was added unmonitored. It only
    switches monitoring on. It never unmonitors or deletes anything.
    """
    matches = api("RADARR", "GET", "movie", params={"tmdbId": tmdb_id})
    if not matches:
        raise ArrError(f"No movie with TMDB id {tmdb_id} is in Radarr. Use add_movie first.")
    movie = matches[0]
    label = f"{movie.get('title')} ({movie.get('year')})"
    if movie.get("hasFile"):
        return f"{label} is already downloaded."

    if not movie.get("monitored"):
        movie["monitored"] = True
        api("RADARR", "PUT", f"movie/{movie['id']}", body=movie)
    api("RADARR", "POST", "command", body={"name": "MoviesSearch", "movieIds": [movie["id"]]})
    return f"Now monitoring {label} and started a search."


@mcp.tool()
def search_show(title: str) -> list[dict]:
    """Search Sonarr for TV series matching a title. Returns candidates with tvdb_id.

    Call this before add_show. If more than one plausible match comes back,
    ask the user which one they mean.
    """
    results = api("SONARR", "GET", "series/lookup", params={"term": title})
    return [
        {
            "title": series.get("title"),
            "year": series.get("year"),
            "tvdb_id": series.get("tvdbId"),
            "network": series.get("network"),
            "status": series.get("status"),
            "genres": series.get("genres"),
            "original_language": (series.get("originalLanguage") or {}).get("name"),
            "season_count": series.get("statistics", {}).get("seasonCount"),
            "in_library": bool(series.get("id")),
            "overview": (series.get("overview") or "")[:200],
        }
        for series in results[:MAX_SEARCH_RESULTS]
    ]


@mcp.tool()
def add_show(
    tvdb_id: int,
    seasons: list[int] | None = None,
    category: Literal["tv", "anime"] = "tv",
) -> str:
    """Add a TV series to Sonarr by TVDB id and start a search for missing episodes.

    seasons: season numbers to monitor, for example [1] or [2, 3]. Leave empty
    to monitor every season (specials excluded). Upcoming shows are added and
    episodes are grabbed as they air.

    category picks the library folder: "anime" for Japanese animated series
    (also sets Sonarr's anime series type), "tv" for everything else. Use the
    genres and original_language from search_show to decide, and ask the user
    if it is unclear.
    """
    results = api("SONARR", "GET", "series/lookup", params={"term": f"tvdb:{tvdb_id}"})
    if not results:
        raise ArrError(f"Sonarr found no series with TVDB id {tvdb_id}")
    series = results[0]
    label = f"{series.get('title')} ({series.get('year')})"
    if series.get("id"):
        return f"{label} is already in Sonarr. Use monitor_seasons to get more seasons of it."

    for season in series.get("seasons", []):
        number = season["seasonNumber"]
        season["monitored"] = number in seasons if seasons else number > 0

    folder = root_folder("SONARR", category)
    series.update(
        {
            "qualityProfileId": quality_profile_id("SONARR", category),
            "rootFolderPath": folder,
            "monitored": True,
            "seasonFolder": True,
            "addOptions": {"searchForMissingEpisodes": True},
        }
    )
    if category == "anime":
        series["seriesType"] = "anime"
    api("SONARR", "POST", "series", body=series)
    scope = f"seasons {sorted(seasons)}" if seasons else "all seasons"
    return f"Added {label} to Sonarr in {folder}, monitoring {scope}, and started a search."


@mcp.tool()
def monitor_seasons(tvdb_id: int, seasons: list[int]) -> str:
    """Start monitoring more seasons of a series already in Sonarr and search for them.

    Use this when add_show reports the series is already in the library, for
    example "get season 2" after season 1 was added earlier. It only switches
    seasons on. It never unmonitors or deletes anything.
    """
    matches = api("SONARR", "GET", "series", params={"tvdbId": tvdb_id})
    if not matches:
        raise ArrError(f"No series with TVDB id {tvdb_id} is in Sonarr. Use add_show first.")
    series = matches[0]
    label = f"{series.get('title')} ({series.get('year')})"

    available = {season["seasonNumber"] for season in series.get("seasons", [])}
    unknown = sorted(set(seasons) - available)
    if unknown:
        raise ArrError(f"{label} has no season {unknown}. Available: {sorted(available)}")

    for season in series["seasons"]:
        if season["seasonNumber"] in seasons:
            season["monitored"] = True
    series["monitored"] = True
    api("SONARR", "PUT", f"series/{series['id']}", body=series)

    for number in sorted(set(seasons)):
        command = {"name": "SeasonSearch", "seriesId": series["id"], "seasonNumber": number}
        api("SONARR", "POST", "command", body=command)
    return f"Now monitoring seasons {sorted(set(seasons))} of {label} and started a search."


@mcp.tool()
def queue_status() -> dict:
    """Show what Sonarr and Radarr are currently downloading, one entry per download.

    For Sonarr, episode_count is how many episodes that download covers (a
    season pack is a single download with many episodes).
    """
    status = {}
    for service in ("SONARR", "RADARR"):
        try:
            queue = api(service, "GET", "queue", params={"pageSize": QUEUE_PAGE_SIZE})
        except ArrError as error:
            status[service.lower()] = f"unavailable: {error}"
            continue
        status[service.lower()] = group_by_download(
            queue.get("records", []), count_episodes=service == "SONARR"
        )
    return status


def group_by_download(records: list[dict], count_episodes: bool) -> list[dict]:
    """Collapse queue rows that belong to the same download into one entry.

    Sonarr returns a season pack as one row per episode, all sharing a
    downloadId, so without this a single pack fills the whole list.
    """
    downloads = {}
    for item in records:
        key = item.get("downloadId") or item.get("title")
        if key not in downloads:
            downloads[key] = {
                "title": item.get("title"),
                "status": item.get("status"),
                "percent_done": percent_done(item),
                "time_left": item.get("timeleft"),
            }
            if count_episodes:
                downloads[key]["episode_count"] = 0
        if count_episodes:
            downloads[key]["episode_count"] += 1
    return list(downloads.values())


def percent_done(item: dict) -> float | None:
    size = item.get("size") or 0
    if not size:
        return None
    return round(100 * (size - (item.get("sizeleft") or 0)) / size, 1)


if __name__ == "__main__":
    mcp.run()
