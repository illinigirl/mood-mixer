"""Audio-feature enrichment — the workaround for Spotify's deprecated endpoint.

Spotify restricted its `/audio-features` endpoint in Nov 2024, so a new app
can't read energy/valence/tempo for a track. This module fills that gap with
community-computed features, cached forever in SQLite keyed by Spotify track id:

  1. AcousticBrainz (https://acousticbrainz.org) — community features computed
     from MusicBrainz. Frozen dataset (no submissions since 2022) but ~28M
     tracks, great for pre-2022 music. Provides tempo, danceability, mood tags,
     and derivable energy/valence. Free, no key. Needs an MBID, resolved via a
     MusicBrainz query.
  2. GetSongBPM (https://getsongbpm.com) — community BPM, covers newer/niche
     tracks AcousticBrainz misses. Free, needs MOODMIXER_GETSONGBPM_KEY. BPM only.

A miss is cached too, so we never re-fetch a track that yielded nothing. Only
this module and store.py touch I/O; the mood engine stays pure.

This is the headline of the project: Spotify took mood data away; we rebuilt it
from open sources.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# Cache lives in the MCP data dir (same env var store.py uses) so a reviewer's
# experiments never touch the repo.
USER_AGENT = os.environ.get(
    "MOODMIXER_FEATURES_UA",
    "mood-mixer/0.1 ( https://github.com/ )",
)
GETSONGBPM_API_KEY = os.environ.get("MOODMIXER_GETSONGBPM_KEY", "")

MB_MIN_INTERVAL = 1.05   # MusicBrainz throttles at 1 req/s per UA
AB_MIN_INTERVAL = 0.3
GSB_MIN_INTERVAL = 1.05

MUSICBRAINZ_BASE = "https://musicbrainz.org/ws/2"
ACOUSTICBRAINZ_BASE = "https://acousticbrainz.org/api/v1"
GETSONGBPM_BASE = "https://api.getsongbpm.com"

SCHEMA = """
CREATE TABLE IF NOT EXISTS track_features (
    track_id      TEXT PRIMARY KEY,
    artist        TEXT,
    title         TEXT,
    mbid          TEXT,
    isrc          TEXT,
    tempo         REAL,
    energy        REAL,
    danceability  REAL,
    valence       REAL,
    acousticness  REAL,
    loudness      REAL,
    mood_tags     TEXT,
    source        TEXT,
    fetched_at    TEXT NOT NULL,
    notes         TEXT
);
"""


def db_path() -> Path:
    d = Path(os.environ.get("MOODMIXER_DATA_DIR", Path.home() / ".mood-mixer"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "track_features.db"


def connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def _cursor():
    conn = connect()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema() -> None:
    with _cursor() as c:
        c.executescript(SCHEMA)


# ---------- HTTP helpers ----------------------------------------------------

_last_hit: dict[str, float] = {}


def _throttle(host_key: str, min_interval: float) -> None:
    now = time.monotonic()
    delta = now - _last_hit.get(host_key, 0)
    if delta < min_interval:
        time.sleep(min_interval - delta)
    _last_hit[host_key] = time.monotonic()


def _http_get_json(url: str, host_key: str, min_interval: float, timeout: int = 15):
    _throttle(host_key, min_interval)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (trusted hosts)
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        return None
    except Exception:
        return None


# ---------- MusicBrainz MBID resolution -------------------------------------

def resolve_mbid(artist: str, title: str, isrc: str | None = None) -> str | None:
    """Find a MusicBrainz recording id. ISRC is most reliable; otherwise query
    artist + recording name (parentheticals stripped to match the studio cut)."""
    if isrc:
        url = f"{MUSICBRAINZ_BASE}/isrc/{urllib.parse.quote(isrc)}?fmt=json"
        j = _http_get_json(url, "mb", MB_MIN_INTERVAL)
        recordings = (j or {}).get("recordings") or []
        if recordings:
            return recordings[0].get("id")
    if not (artist and title):
        return None
    clean_title = title.split(" - ")[0].split(" (")[0]
    q = f'artist:"{artist}" AND recording:"{clean_title}"'
    url = f"{MUSICBRAINZ_BASE}/recording?query={urllib.parse.quote(q)}&limit=3&fmt=json"
    j = _http_get_json(url, "mb", MB_MIN_INTERVAL)
    recordings = (j or {}).get("recordings") or []
    return recordings[0].get("id") if recordings else None


# ---------- AcousticBrainz --------------------------------------------------

def _ab_energy_valence(highlevel: dict) -> tuple[float, float]:
    """Derive energy + valence from AcousticBrainz mood classifiers, which don't
    expose those as numbers directly. Each classifier is a {value, probability}
    pair; we nudge from a 0.5 baseline by how confidently each mood fires."""
    def prob(key: str, positive: str) -> float:
        e = highlevel.get(key) or {}
        v = e.get("value")
        p = e.get("probability", 0)
        return p if v == positive else (1 - p if v else 0.5)

    en = 0.5
    en += 0.25 * (prob("mood_aggressive", "aggressive") - 0.5) * 2
    en += 0.20 * (prob("mood_happy", "happy") - 0.5) * 2
    en += 0.15 * (prob("danceability", "danceable") - 0.5) * 2
    en -= 0.20 * (prob("mood_relaxed", "relaxed") - 0.5) * 2
    en -= 0.20 * (prob("mood_sad", "sad") - 0.5) * 2

    va = 0.5
    va += 0.30 * (prob("mood_happy", "happy") - 0.5) * 2
    va += 0.20 * (prob("mood_party", "party") - 0.5) * 2
    va -= 0.30 * (prob("mood_sad", "sad") - 0.5) * 2

    return max(0.0, min(1.0, en)), max(0.0, min(1.0, va))


def fetch_acousticbrainz(mbid: str) -> dict | None:
    """Pull high-level + low-level summaries for an MBID → a partial features
    dict, or None if AcousticBrainz doesn't have it."""
    if not mbid:
        return None
    high = _http_get_json(f"{ACOUSTICBRAINZ_BASE}/{mbid}/high-level", "ab", AB_MIN_INTERVAL)
    low = _http_get_json(f"{ACOUSTICBRAINZ_BASE}/{mbid}/low-level", "ab", AB_MIN_INTERVAL)
    if not high and not low:
        return None

    out: dict = {"mbid": mbid, "source": "acousticbrainz"}
    if low:
        rhythm = low.get("rhythm") or {}
        if "bpm" in rhythm:
            out["tempo"] = float(rhythm["bpm"])
        if "danceability" in rhythm:
            # AB rhythm.danceability is 0-3; normalize to the 0-1 the filter expects.
            out["danceability"] = max(0.0, min(1.0, float(rhythm["danceability"]) / 3.0))
        lowlevel = low.get("lowlevel") or {}
        if "average_loudness" in lowlevel:
            out["loudness"] = float(lowlevel["average_loudness"])
    if high:
        hl = high.get("highlevel") or {}
        energy, valence = _ab_energy_valence(hl)
        out["energy"] = energy
        out["valence"] = valence
        mood_tags = [
            e.get("value") for e in hl.values()
            if isinstance(e, dict) and e.get("probability", 0) > 0.6
            and isinstance(e.get("value"), str) and not e["value"].startswith("not_")
        ]
        if mood_tags:
            out["mood_tags"] = mood_tags
    return out if any(k in out for k in ("tempo", "energy", "danceability")) else None


# ---------- GetSongBPM -----------------------------------------------------

def fetch_getsongbpm(artist: str, title: str) -> dict | None:
    """Look up BPM on GetSongBPM. Tempo only — no energy/mood."""
    if not GETSONGBPM_API_KEY or not artist or not title:
        return None
    lookup = f"song:{title} artist:{artist}"
    url = (f"{GETSONGBPM_BASE}/search/?api_key={GETSONGBPM_API_KEY}"
           f"&type=both&lookup={urllib.parse.quote(lookup)}")
    j = _http_get_json(url, "gsb", GSB_MIN_INTERVAL)
    if not j or "search" not in j:
        return None
    results = j["search"]
    if isinstance(results, dict):
        results = [results]
    if not results:
        return None
    try:
        return {"tempo": float(results[0].get("tempo")), "source": "getsongbpm"}
    except (TypeError, ValueError):
        return None


# ---------- cache-aware enrichment -----------------------------------------

def get_cached(track_id: str) -> dict | None:
    with _cursor() as c:
        c.execute("SELECT * FROM track_features WHERE track_id = ?", (track_id,))
        row = c.fetchone()
        return dict(row) if row else None


def cached_map() -> dict[str, dict]:
    """All non-miss cached features keyed by track_id, for bulk hydration of a
    library. Empty dict when the cache doesn't exist yet."""
    if not db_path().exists():
        return {}
    try:
        with _cursor() as c:
            c.execute("SELECT * FROM track_features WHERE source != 'miss'")
            return {r["track_id"]: dict(r) for r in c.fetchall()}
    except sqlite3.OperationalError:
        return {}


def _save(track_id: str, artist: str, title: str, data: dict, isrc: str | None = None) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with _cursor() as c:
        c.execute("""
            INSERT INTO track_features
                (track_id, artist, title, mbid, isrc, tempo, energy, danceability,
                 valence, acousticness, loudness, mood_tags, source, fetched_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_id) DO UPDATE SET
                mbid=COALESCE(excluded.mbid, track_features.mbid),
                tempo=COALESCE(excluded.tempo, track_features.tempo),
                energy=COALESCE(excluded.energy, track_features.energy),
                danceability=COALESCE(excluded.danceability, track_features.danceability),
                valence=COALESCE(excluded.valence, track_features.valence),
                acousticness=COALESCE(excluded.acousticness, track_features.acousticness),
                loudness=COALESCE(excluded.loudness, track_features.loudness),
                mood_tags=COALESCE(excluded.mood_tags, track_features.mood_tags),
                source=excluded.source, fetched_at=excluded.fetched_at
        """, (
            track_id, artist, title, data.get("mbid"), isrc,
            data.get("tempo"), data.get("energy"), data.get("danceability"),
            data.get("valence"), data.get("acousticness"), data.get("loudness"),
            json.dumps(data["mood_tags"]) if data.get("mood_tags") else None,
            data.get("source", "miss"), now, None,
        ))


def enrich(track_id: str, artist: str, title: str, isrc: str | None = None) -> dict | None:
    """Fetch features from the community sources and cache the result. Records a
    'miss' row (so we don't retry) when nothing useful is found."""
    init_schema()
    mbid = resolve_mbid(artist, title, isrc=isrc)
    ab = fetch_acousticbrainz(mbid) if mbid else None
    merged = dict(ab) if ab else {}
    if mbid and "mbid" not in merged:
        merged["mbid"] = mbid
    if not merged.get("tempo"):
        gsb = fetch_getsongbpm(artist, title)
        if gsb:
            merged.update(gsb)
            merged["source"] = "mixed" if ab else "getsongbpm"

    useful = any(merged.get(k) is not None for k in ("tempo", "energy", "danceability"))
    if not useful:
        miss = {"source": "miss"}
        if merged.get("mbid"):
            miss["mbid"] = merged["mbid"]
        _save(track_id, artist, title, miss, isrc=isrc)
        return None
    _save(track_id, artist, title, merged, isrc=isrc)
    return merged


def get_or_enrich(track_id: str, artist: str, title: str, isrc: str | None = None) -> dict | None:
    """Cache-first lookup; enrich only on a true miss. Never re-fetches a row
    that previously resolved or that we already recorded as a miss."""
    cached = get_cached(track_id)
    if cached:
        return cached if cached.get("source") != "miss" else None
    return enrich(track_id, artist, title, isrc=isrc)
