"""The mood engine — pure, no I/O, fully unit-tested on stdlib.

This is the heart of mood-mixer and the answer to a real problem: Spotify
deprecated its `audio-features` endpoint in Nov 2024, so we can't ask Spotify
"how energetic is this track?" anymore. Instead each track arrives carrying
whatever audio features we could source from community data (features.py), and
this module:

  1. fills any missing feature axes from a coarse genre→mood estimate
     (`GENRE_MOOD` / `estimate_mood_from_genres`), so a track is never dropped
     just because community data lacked it;
  2. filters a library against a named mood preset (`MOOD_PRESETS`) — exact,
     threshold-based, deterministic (this is the "exact compute" pillar);
  3. assembles a varied playlist (`build_mix`): dedup, one track per artist,
     capped to a length.

Ported from the dashboard's smart-mix (SMART_MIX_PRESETS + GENRE_MOOD). The
LLM picks the mood and narrates; the matching math lives here and never moves
into the model.
"""

from __future__ import annotations

import random

from .models import Track

# Mood presets: a label + the feature thresholds that define the mood. tempo is
# BPM; the rest are 0..1. Tunable — these are starting points, not gospel.
MOOD_PRESETS: dict[str, dict] = {
    "chill": {"label": "Chill Evening",
              "criteria": {"energy_max": 0.45, "valence_min": 0.3, "tempo_max": 120}},
    "morning": {"label": "Morning Energy",
                "criteria": {"energy_min": 0.6, "valence_min": 0.5, "tempo_min": 100}},
    "focus": {"label": "Focus",
              "criteria": {"energy_max": 0.5, "acousticness_min": 0.3,
                           "speechiness_max": 0.1, "instrumentalness_min": 0.1}},
    "roadtrip": {"label": "Road Trip",
                 "criteria": {"energy_min": 0.6, "danceability_min": 0.5, "valence_min": 0.4}},
    "workout": {"label": "Workout",
                "criteria": {"energy_min": 0.75, "tempo_min": 120}},
    "melancholy": {"label": "Melancholy",
                   "criteria": {"energy_max": 0.45, "valence_max": 0.35}},
    "party": {"label": "Party",
              "criteria": {"energy_min": 0.7, "danceability_min": 0.65, "valence_min": 0.5}},
}

# Coarse genre → {energy, valence} estimate, used to fill features for tracks
# community data couldn't cover. Keys are lowercase Spotify artist genres.
GENRE_MOOD: dict[str, dict] = {
    # High energy
    "punk": {"energy": 0.85, "valence": 0.6}, "punk rock": {"energy": 0.85, "valence": 0.6},
    "metal": {"energy": 0.9, "valence": 0.3}, "hard rock": {"energy": 0.8, "valence": 0.5},
    "power pop": {"energy": 0.75, "valence": 0.7}, "garage rock": {"energy": 0.8, "valence": 0.55},
    "pop punk": {"energy": 0.8, "valence": 0.65}, "new wave": {"energy": 0.7, "valence": 0.6},
    # Medium energy
    "rock": {"energy": 0.65, "valence": 0.5}, "classic rock": {"energy": 0.65, "valence": 0.55},
    "alternative rock": {"energy": 0.6, "valence": 0.45}, "indie rock": {"energy": 0.6, "valence": 0.5},
    "psychedelic rock": {"energy": 0.55, "valence": 0.5}, "art rock": {"energy": 0.55, "valence": 0.4},
    "jangle pop": {"energy": 0.6, "valence": 0.6}, "baroque pop": {"energy": 0.5, "valence": 0.5},
    "britpop": {"energy": 0.65, "valence": 0.55}, "indie": {"energy": 0.55, "valence": 0.5},
    # Low energy
    "folk": {"energy": 0.35, "valence": 0.45}, "indie folk": {"energy": 0.35, "valence": 0.5},
    "singer-songwriter": {"energy": 0.35, "valence": 0.4}, "americana": {"energy": 0.4, "valence": 0.45},
    "slowcore": {"energy": 0.25, "valence": 0.3}, "dream pop": {"energy": 0.35, "valence": 0.45},
    "shoegaze": {"energy": 0.45, "valence": 0.35}, "ambient": {"energy": 0.2, "valence": 0.4},
    "lullaby": {"energy": 0.15, "valence": 0.5}, "alt country": {"energy": 0.4, "valence": 0.45},
    "art pop": {"energy": 0.5, "valence": 0.5},
    # Upbeat
    "pop": {"energy": 0.7, "valence": 0.65}, "dance pop": {"energy": 0.8, "valence": 0.7},
    "funk": {"energy": 0.75, "valence": 0.7}, "soul": {"energy": 0.55, "valence": 0.55},
    "r&b": {"energy": 0.5, "valence": 0.5},
}


def list_presets() -> list[dict]:
    """The available moods, for the list_moods tool / CLI."""
    return [{"key": k, "label": v["label"], "criteria": v["criteria"]}
            for k, v in MOOD_PRESETS.items()]


def estimate_mood_from_genres(genres: list[str]) -> dict | None:
    """Average the energy/valence of any recognized genres. Returns
    {"energy", "valence"} or None when no genre is in GENRE_MOOD."""
    energies, valences = [], []
    for g in genres:
        mood = GENRE_MOOD.get(g.lower())
        if mood:
            energies.append(mood["energy"])
            valences.append(mood["valence"])
    if not energies:
        return None
    return {"energy": sum(energies) / len(energies),
            "valence": sum(valences) / len(valences)}


def resolve_features(track: Track) -> dict | None:
    """Produce a full feature vector for a track, filling missing axes.

    Real (community-sourced) features win; anything missing is backfilled from
    the genre estimate, then from neutral defaults. Returns None only when the
    track has neither real features nor a recognized genre — i.e. nothing to go
    on — so build_mix can skip it rather than guess blindly. Mirrors the
    dashboard's enriched-vs-genre fill so behavior matches the proven version.
    """
    has_real = any(getattr(track, ax) is not None for ax in ("energy", "valence", "tempo"))
    mood = estimate_mood_from_genres(track.genres)
    if not has_real and mood is None:
        return None

    base_e = mood["energy"] if mood else 0.5
    base_v = mood["valence"] if mood else 0.5
    energy = track.energy if track.energy is not None else base_e
    valence = track.valence if track.valence is not None else base_v
    return {
        "energy": energy,
        "valence": valence,
        "tempo": track.tempo if track.tempo is not None else 120.0,
        "danceability": track.danceability if track.danceability is not None else (energy + valence) / 2,
        "acousticness": track.acousticness if track.acousticness is not None else max(0.0, 1 - energy),
        "speechiness": track.speechiness if track.speechiness is not None else 0.05,
        "instrumentalness": track.instrumentalness if track.instrumentalness is not None else 0.1,
    }


# Each criterion → a predicate over the resolved feature vector.
_CHECKS = {
    "energy_min": lambda f, v: f["energy"] >= v,
    "energy_max": lambda f, v: f["energy"] <= v,
    "valence_min": lambda f, v: f["valence"] >= v,
    "valence_max": lambda f, v: f["valence"] <= v,
    "tempo_min": lambda f, v: f["tempo"] >= v,
    "tempo_max": lambda f, v: f["tempo"] <= v,
    "danceability_min": lambda f, v: f["danceability"] >= v,
    "acousticness_min": lambda f, v: f["acousticness"] >= v,
    "speechiness_max": lambda f, v: f["speechiness"] <= v,
    "instrumentalness_min": lambda f, v: f["instrumentalness"] >= v,
}


def track_matches(features: dict, criteria: dict) -> bool:
    """True if a resolved feature vector satisfies every threshold in criteria."""
    return all(_CHECKS[key](features, val) for key, val in criteria.items() if key in _CHECKS)


def build_mix(tracks: list[Track], preset: str | None = None, criteria: dict | None = None,
              limit: int = 50, max_per_artist: int = 1, shuffle_seed: int | None = None) -> list[Track]:
    """Filter `tracks` to a mood and assemble a varied playlist.

    Pass `preset` (a key in MOOD_PRESETS) or an explicit `criteria` dict. Variety
    rules: dedup by id, at most `max_per_artist` per artist, capped to `limit`.
    Ordering is deterministic (stable sort by artist, name) unless `shuffle_seed`
    is given, in which case it's a seeded shuffle — so callers get variety while
    tests stay reproducible.
    """
    if criteria is None:
        if preset not in MOOD_PRESETS:
            raise ValueError(f"unknown mood preset: {preset!r} (known: {', '.join(MOOD_PRESETS)})")
        criteria = MOOD_PRESETS[preset]["criteria"]

    matched = [t for t in tracks if (f := resolve_features(t)) is not None and track_matches(f, criteria)]

    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(matched)
    else:
        matched.sort(key=lambda t: (t.artist.lower(), t.name.lower()))

    seen_ids: set[str] = set()
    per_artist: dict[str, int] = {}
    out: list[Track] = []
    for t in matched:
        if t.id in seen_ids:
            continue
        artist_key = t.artist.lower()
        if per_artist.get(artist_key, 0) >= max_per_artist:
            continue
        seen_ids.add(t.id)
        per_artist[artist_key] = per_artist.get(artist_key, 0) + 1
        out.append(t)
        if len(out) >= limit:
            break
    return out
