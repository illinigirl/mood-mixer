"""Local persistence — the library cache, and hydrating it with cached features.

The bundled `data/sample-library.json` ships read-only so the repo runs cold.
Your real liked-library cache + the features DB live in the data dir
(MOODMIXER_DATA_DIR, default ~/.mood-mixer), gitignored, so personal data never
lands in a commit. Network I/O lives in spotify.py / features.py; this module is
just the filesystem + the merge that joins library records to cached features.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import features
from .models import Track

_PKG_ROOT = Path(__file__).resolve().parents[2]  # repo root when run from a clone

# Audio-feature columns the features DB can supply for a track.
_FEATURE_KEYS = ("energy", "valence", "tempo", "danceability", "acousticness")


def data_dir() -> Path:
    d = Path(os.environ.get("MOODMIXER_DATA_DIR", Path.home() / ".mood-mixer"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def sample_library_path() -> Path:
    return Path(os.environ.get("MOODMIXER_SAMPLE", _PKG_ROOT / "data" / "sample-library.json"))


def library_cache_path() -> Path:
    return data_dir() / "library-cache.json"


def library_source() -> str:
    """'cache' once you've refreshed from Spotify, else 'sample' (the bundle)."""
    return "cache" if library_cache_path().exists() else "sample"


def _read_tracks() -> list[dict]:
    path = library_cache_path() if library_cache_path().exists() else sample_library_path()
    return json.loads(path.read_text()).get("tracks", [])


def _merge_features(track: dict, row: dict | None) -> dict:
    """Overlay cached audio features (when present) onto a library record."""
    if not row:
        return track
    out = dict(track)
    for key in _FEATURE_KEYS:
        if row.get(key) is not None:
            out[key] = row[key]
    out["features_source"] = row.get("source")
    return out


def load_library(hydrate: bool = True) -> list[Track]:
    """The library as Track objects: cache if refreshed, else the bundled sample.
    With `hydrate`, overlays any community features cached in the features DB —
    so a refreshed library (which stores only ids/names/genres) gains the
    energy/valence/tempo needed by the mood engine."""
    raw = _read_tracks()
    if hydrate:
        feats = features.cached_map()
        if feats:
            raw = [_merge_features(t, feats.get(t["id"])) for t in raw]
    return [Track.from_dict(t) for t in raw]


def save_library(records: list[dict]) -> int:
    """Atomically write the liked-library cache (temp file + os.replace, so a
    failed fetch never corrupts an existing cache). Returns the count."""
    path = library_cache_path()
    fd, tmp = tempfile.mkstemp(prefix=".lib-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"tracks": records}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return len(records)


# ── Preferences: persisted "skip from now on" rules ─────────────────
# Cross-session memory (MCP pillar #1). Artists/genres are stored lowercased so
# matching is case-insensitive. The LLM resolves fuzzy rules ("Mount Eerie songs
# about death") to concrete track ids before saving; the note keeps the rule in
# plain English for transparency.

_PREF_KEYS = ("excluded_track_ids", "excluded_artists", "excluded_genres", "notes")


def preferences_path() -> Path:
    return data_dir() / "preferences.json"


def load_preferences() -> dict:
    p = preferences_path()
    prefs = json.loads(p.read_text()) if p.exists() else {}
    for k in _PREF_KEYS:
        prefs.setdefault(k, [])
    return prefs


def save_preferences(prefs: dict) -> None:
    preferences_path().write_text(json.dumps(prefs, indent=2, ensure_ascii=False))


def _merge_unique(existing: list, additions) -> list:
    out = list(existing)
    for a in additions or []:
        if a not in out:
            out.append(a)
    return out


def add_exclusion(track_ids=None, artists=None, genres=None, note=None) -> dict:
    """Persist a standing exclusion. Returns the updated preferences."""
    prefs = load_preferences()
    prefs["excluded_track_ids"] = _merge_unique(prefs["excluded_track_ids"], track_ids)
    prefs["excluded_artists"] = _merge_unique(prefs["excluded_artists"], [a.lower() for a in (artists or [])])
    prefs["excluded_genres"] = _merge_unique(prefs["excluded_genres"], [g.lower() for g in (genres or [])])
    if note:
        prefs["notes"] = _merge_unique(prefs["notes"], [note])
    save_preferences(prefs)
    return prefs


def remove_exclusion(track_ids=None, artists=None, genres=None) -> dict:
    """Drop standing exclusions (undo). Returns the updated preferences."""
    prefs = load_preferences()
    drop_ids = set(track_ids or [])
    drop_artists = {a.lower() for a in (artists or [])}
    drop_genres = {g.lower() for g in (genres or [])}
    prefs["excluded_track_ids"] = [x for x in prefs["excluded_track_ids"] if x not in drop_ids]
    prefs["excluded_artists"] = [x for x in prefs["excluded_artists"] if x not in drop_artists]
    prefs["excluded_genres"] = [x for x in prefs["excluded_genres"] if x not in drop_genres]
    save_preferences(prefs)
    return prefs


# ── Recent-play history: a cooldown so back-to-back mixes vary ───────
# Remembers which tracks each playlist used (with a timestamp); a cooldown then
# excludes recently-used tracks from the next builds, so consecutive playlists
# don't repeat the same songs. Scoped to what mood-mixer itself put in playlists.

def recent_path() -> Path:
    return data_dir() / "recent.json"


def load_recent() -> list[dict]:
    p = recent_path()
    return json.loads(p.read_text()) if p.exists() else []


def record_played(track_ids, when: datetime | None = None) -> None:
    """Remember tracks just put into a playlist. Prunes entries older than 60
    days so the file stays small. `when` is injectable for tests."""
    now = when or datetime.now(UTC)
    recent = load_recent()
    recent.extend({"id": tid, "ts": now.isoformat()} for tid in track_ids)
    cutoff = (now - timedelta(days=60)).isoformat()
    recent = [e for e in recent if e.get("ts", "") >= cutoff]
    recent_path().write_text(json.dumps(recent))


def recent_track_ids(within_hours: float, now: datetime | None = None) -> set[str]:
    """Track ids played within the cooldown window (empty if cooldown <= 0).
    Compares ISO-8601 UTC timestamps, which sort chronologically as strings."""
    if within_hours <= 0:
        return set()
    cutoff = ((now or datetime.now(UTC)) - timedelta(hours=within_hours)).isoformat()
    return {e["id"] for e in load_recent() if e.get("ts", "") >= cutoff}
