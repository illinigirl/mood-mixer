"""The MCP server — thin tools over the pure mood engine + the I/O modules.

All matching logic lives in moods.py (pure, unit-tested); this file only wires
it to MCP tools, the library cache (store.py), Spotify (spotify.py), and feature
enrichment (features.py). The CLI (cli.py) is a second adapter over the same
calls.

Tool surface (v1):
  list_moods            — the available mood presets
  get_library_status    — how many tracks are cached + feature coverage
  preview_mix           — the tracks a mood would select (no side effect)
  create_playlist       — build the mix AND save it as a real Spotify playlist
  refresh_library       — pull your liked songs into the local cache
  enrich_features       — backfill community audio features for the cache

Dual transport: stdio by default (Claude Desktop), or streamable-HTTP via
--http / MOOD_MIXER_HTTP=1. Auth (a one-time browser flow) is a CLI step, not a
tool — see `python -m moodmixer.cli authorize`.
"""

from __future__ import annotations

import argparse
import os

from mcp.server.fastmcp import FastMCP

from . import features, moods, spotify, store

mcp = FastMCP("mood-mixer")

# Feature sources that count as "real" (vs. a genre estimate or nothing).
_REAL_SOURCES = {"acousticbrainz", "getsongbpm", "mixed", "sample"}


@mcp.tool()
def list_moods() -> dict:
    """The available mood presets and the audio-feature thresholds that define
    each — the menu for preview_mix / create_playlist."""
    return {"moods": moods.list_presets()}


@mcp.tool()
def get_library_status() -> dict:
    """How many tracks are available to mix, where they came from (your refreshed
    cache vs. the bundled sample), and how many have real audio features vs. a
    genre estimate. Low real-feature coverage → run enrich_features."""
    lib = store.load_library()
    real = sum(1 for t in lib if t.features_source in _REAL_SOURCES)
    estimated = sum(1 for t in lib
                    if t.features_source not in _REAL_SOURCES
                    and moods.estimate_mood_from_genres(t.genres) is not None)
    return {
        "track_count": len(lib),
        "source": store.library_source(),
        "real_features": real,
        "genre_estimated": estimated,
        "no_features": len(lib) - real - estimated,
        "spotify_authorized": spotify.get_access_token() is not None,
    }


def _effective_excludes(exclude_track_ids, exclude_artists, exclude_genres,
                        allow_artists, allow_genres, cooldown_days=0.0):
    """Merge saved preferences + a recent-play cooldown + this call's ad-hoc
    excludes, then drop any the caller explicitly allows this time (the "...unless
    I ask" override)."""
    prefs = store.load_preferences()
    ids = set(prefs["excluded_track_ids"]) | set(exclude_track_ids or [])
    if cooldown_days and cooldown_days > 0:
        ids |= store.recent_track_ids(cooldown_days * 24)
    artists = {a.lower() for a in prefs["excluded_artists"]} | {a.lower() for a in (exclude_artists or [])}
    genres = {g.lower() for g in prefs["excluded_genres"]} | {g.lower() for g in (exclude_genres or [])}
    artists -= {a.lower() for a in (allow_artists or [])}
    genres -= {g.lower() for g in (allow_genres or [])}
    return ids, artists, genres


def _mix(preset, limit, shuffle_seed, ex_ids, ex_artists, ex_genres):
    lib = store.load_library()
    crit = moods.MOOD_PRESETS[preset]["criteria"]
    strict = 0
    for t in lib:
        if t.id in ex_ids or t.artist.lower() in ex_artists:
            continue
        if ex_genres and any(g in ex_genres for g in t.genres):
            continue
        f = moods.resolve_features(t)
        if f and moods.track_matches(f, crit):
            strict += 1
    mix = moods.build_mix(lib, preset, limit=limit, shuffle_seed=shuffle_seed,
                          exclude_track_ids=ex_ids, exclude_artists=ex_artists, exclude_genres=ex_genres)
    return mix, strict


@mcp.tool()
def preview_mix(preset: str, limit: int = 30, exclude_artists: list[str] | None = None,
                exclude_genres: list[str] | None = None, exclude_track_ids: list[str] | None = None,
                allow_artists: list[str] | None = None, allow_genres: list[str] | None = None,
                cooldown_days: float = 7, shuffle_seed: int | None = None) -> dict:
    """Show the tracks a mood would select from your library — WITHOUT creating a
    playlist (the grounding step). Tracks are ranked by FIT, so you always get a
    full list (the closest N), not just exact matches; `strict_matches` reports how
    many fully fit, `selected` how many were returned.

    Honors your saved exclusions (add_exclusion) and a recent-play `cooldown_days`
    (default 7 → skip tracks used in playlists in the last week, so back-to-back
    mixes vary; set 0 to ignore). Use `exclude_artists/genres/track_ids` to drop
    more just this time, or `allow_artists/genres` to override a saved rule for this
    one request (e.g. lullabies are off by default — include them now). `preset` is
    one of list_moods."""
    if preset not in moods.MOOD_PRESETS:
        return {"error": f"unknown mood {preset!r}", "known": list(moods.MOOD_PRESETS)}
    ids, artists, genres = _effective_excludes(exclude_track_ids, exclude_artists, exclude_genres,
                                               allow_artists, allow_genres, cooldown_days)
    mix, strict = _mix(preset, limit, shuffle_seed, ids, artists, genres)
    return {
        "mood": preset,
        "strict_matches": strict,
        "selected": len(mix),
        "tracks": [{"name": t.name, "artist": t.artist, "features_source": t.features_source}
                   for t in mix],
    }


@mcp.tool()
def create_playlist(preset: str, name: str | None = None, limit: int = 30,
                    exclude_artists: list[str] | None = None, exclude_genres: list[str] | None = None,
                    exclude_track_ids: list[str] | None = None, allow_artists: list[str] | None = None,
                    allow_genres: list[str] | None = None, cooldown_days: float = 7,
                    shuffle_seed: int | None = None, replace_playlist_id: str | None = None) -> dict:
    """Build the mood mix AND save it as a real (private) Spotify playlist — the
    payoff (the side effect plain Claude can't do). Optionally pass `name` for the
    playlist's title (defaults to the mood label). Honors saved exclusions and a
    recent-play `cooldown_days` (default 7, so back-to-back playlists don't repeat
    tracks; 0 to ignore); `exclude_*` adds more, `allow_*` overrides a saved rule
    for this request. Pass `replace_playlist_id` to REBUILD an existing playlist in
    place (keeps its name + URL, swaps the tracks). The tracks used are remembered
    to feed the cooldown. Needs Spotify authorization (run
    `python -m moodmixer.cli authorize` once). Returns the URL."""
    if preset not in moods.MOOD_PRESETS:
        return {"error": f"unknown mood {preset!r}", "known": list(moods.MOOD_PRESETS)}
    ids, artists, genres = _effective_excludes(exclude_track_ids, exclude_artists, exclude_genres,
                                               allow_artists, allow_genres, cooldown_days)
    mix, _ = _mix(preset, limit, shuffle_seed, ids, artists, genres)
    if not mix:
        return {"error": f"no tracks available for '{preset}' after exclusions/cooldown — loosen "
                         "filters, lower cooldown_days, or run enrich_features for more coverage"}
    label = moods.MOOD_PRESETS[preset]["label"]
    playlist_name = name or f"{label} (mood-mixer)"
    uris = [t.uri for t in mix]
    try:
        if replace_playlist_id:
            result = spotify.replace_playlist_tracks(replace_playlist_id, uris)
            # Only rename when a name is explicitly given — never clobber the
            # existing title on a plain rebuild.
            if name:
                spotify.update_playlist_details(
                    replace_playlist_id, name=name,
                    description=f"Built by mood-mixer from your liked library — mood: {label}.")
            out = {"updated": True, "mood": preset, "renamed": bool(name),
                   "playlist_url": f"https://open.spotify.com/playlist/{replace_playlist_id}", **result}
            if name:
                out["name"] = name
        else:
            result = spotify.create_playlist(
                playlist_name, uris,
                description=f"Built by mood-mixer from your liked library — mood: {label}.",
            )
            out = {"created": True, "mood": preset, "name": playlist_name, **result}
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}
    store.record_played([t.id for t in mix])   # remember for the cooldown
    return out


@mcp.tool()
def add_exclusion(track_ids: list[str] | None = None, artists: list[str] | None = None,
                  genres: list[str] | None = None, note: str | None = None) -> dict:
    """Save a standing 'skip from now on' rule — the cross-session memory that makes
    mood-mixer personal. Exclude specific `track_ids` (e.g. the Mount Eerie songs
    about death you named — you identify them, this remembers them), `artists`, or
    `genres` (e.g. "lullaby"). `note` records the rule in plain English. Applied to
    every preview/create automatically; override per-request via the allow_* params."""
    prefs = store.add_exclusion(track_ids=track_ids, artists=artists, genres=genres, note=note)
    return {"saved": True, "excluded_track_ids": len(prefs["excluded_track_ids"]),
            "excluded_artists": prefs["excluded_artists"], "excluded_genres": prefs["excluded_genres"],
            "notes": prefs["notes"]}


@mcp.tool()
def list_preferences() -> dict:
    """Show your saved exclusions (track ids, artists, genres) and the plain-English
    notes behind them."""
    return store.load_preferences()


@mcp.tool()
def remove_exclusion(track_ids: list[str] | None = None, artists: list[str] | None = None,
                     genres: list[str] | None = None) -> dict:
    """Undo a standing exclusion — start allowing an artist/genre/track again."""
    prefs = store.remove_exclusion(track_ids=track_ids, artists=artists, genres=genres)
    return {"updated": True, "excluded_artists": prefs["excluded_artists"],
            "excluded_genres": prefs["excluded_genres"],
            "excluded_track_ids": len(prefs["excluded_track_ids"])}


@mcp.tool()
def refresh_library() -> dict:
    """Pull your Spotify liked songs (with artist genres) into the local cache,
    replacing the bundled sample. Run this once up front, then occasionally.
    Needs Spotify authorization."""
    try:
        records = spotify.fetch_liked_tracks()
    except RuntimeError as e:
        return {"error": str(e)}
    count = store.save_library(records)
    return {"refreshed": True, "track_count": count,
            "note": "run enrich_features next to add audio features"}


@mcp.tool()
def enrich_features(limit: int = 50) -> dict:
    """Backfill community audio features (AcousticBrainz / GetSongBPM) for cached
    tracks that don't have them yet — this is what makes mood matching accurate
    rather than genre-estimated. Rate-limited and slow (~1s/track), so it works
    in capped batches; call repeatedly. Returns how many were enriched."""
    features.init_schema()
    raw = store.load_library(hydrate=False)
    todo = [t for t in raw if features.get_cached(t.id) is None][:max(0, limit)]
    hit = miss = 0
    for t in todo:
        if features.enrich(t.id, t.artist, t.name):
            hit += 1
        else:
            miss += 1
    return {"attempted": len(todo), "enriched": hit, "missed": miss,
            "remaining_uncached": sum(1 for t in raw if features.get_cached(t.id) is None)}


def _resolve_transport(argv=None) -> tuple[str, str, int]:
    """stdio by default; --http / MOOD_MIXER_HTTP=1 → streamable-HTTP. Factored
    out so it's testable without binding a port."""
    parser = argparse.ArgumentParser(prog="mood-mixer", description="mood-mixer MCP server")
    parser.add_argument("--http", action="store_true",
                        help="serve over streamable-HTTP (custom connector) instead of stdio")
    parser.add_argument("--host", default=os.environ.get("MOOD_MIXER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MOOD_MIXER_PORT", "8000")))
    args = parser.parse_args(argv)
    http = args.http or os.environ.get("MOOD_MIXER_HTTP", "").lower() in {"1", "true", "yes"}
    return ("streamable-http" if http else "stdio", args.host, args.port)


def main(argv=None) -> None:
    transport, host, port = _resolve_transport(argv)
    if transport == "streamable-http":
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
