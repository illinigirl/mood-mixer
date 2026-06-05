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
