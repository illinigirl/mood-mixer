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
tool — see `mood-mixer authorize`.
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


def _mix(preset: str, limit: int, shuffle_seed: int | None):
    lib = store.load_library()
    matched = moods.build_mix(lib, preset, limit=len(lib) or 1, max_per_artist=10**6)
    mix = moods.build_mix(lib, preset, limit=limit, shuffle_seed=shuffle_seed)
    return mix, len(matched)


@mcp.tool()
def preview_mix(preset: str, limit: int = 30, shuffle_seed: int | None = None) -> dict:
    """Show the tracks a mood would select from your library — WITHOUT creating a
    playlist. The grounding step: see what you'd get, then call create_playlist.
    `preset` is one of list_moods (chill, morning, focus, roadtrip, workout,
    melancholy, party)."""
    if preset not in moods.MOOD_PRESETS:
        return {"error": f"unknown mood {preset!r}", "known": list(moods.MOOD_PRESETS)}
    mix, matched = _mix(preset, limit, shuffle_seed)
    return {
        "mood": preset,
        "matched": matched,
        "selected": len(mix),
        "tracks": [{"name": t.name, "artist": t.artist, "features_source": t.features_source}
                   for t in mix],
    }


@mcp.tool()
def create_playlist(preset: str, name: str | None = None, limit: int = 30,
                    shuffle_seed: int | None = None) -> dict:
    """Build the mood mix AND save it as a real (private) Spotify playlist — the
    payoff (the side effect plain Claude can't do). Needs Spotify authorization
    (run `mood-mixer authorize` once). Returns the playlist URL."""
    if preset not in moods.MOOD_PRESETS:
        return {"error": f"unknown mood {preset!r}", "known": list(moods.MOOD_PRESETS)}
    mix, _ = _mix(preset, limit, shuffle_seed)
    if not mix:
        return {"error": f"no tracks in your library match '{preset}' — try refresh_library "
                         "or enrich_features to improve coverage"}
    label = moods.MOOD_PRESETS[preset]["label"]
    playlist_name = name or f"{label} (mood-mixer)"
    try:
        result = spotify.create_playlist(
            playlist_name, [t.uri for t in mix],
            description=f"Built by mood-mixer from your liked library — mood: {label}.",
        )
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}
    return {"created": True, "mood": preset, "name": playlist_name, **result}


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
