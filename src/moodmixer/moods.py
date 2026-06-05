"""The mood engine — pure, no I/O, fully unit-tested on stdlib.

This is the heart of mood-mixer and the answer to a real problem: Spotify
deprecated its `audio-features` endpoint in Nov 2024, so we can't ask Spotify
"how energetic is this track?" anymore. Each track arrives carrying whatever
audio features we could source from community data (features.py), and this
module:

  1. fills any missing feature axes from a genre→mood estimate (`GENRE_MOOD` /
     `estimate_mood_from_genres`), now richer: more genres, and tempo estimated
     from energy so a genre-only track isn't stuck at a flat default;
  2. scores a library against a mood by FIT (soft scoring), not a hard yes/no —
     so a mood always returns a full, well-ordered playlist (the closest N),
     instead of just the handful that clear every threshold;
  3. assembles a varied playlist (`build_mix`): honors persisted/ad-hoc
     exclusions, dedup, one track per artist, capped to a length.

Deterministic throughout — the LLM picks the mood and narrates; the matching
math never moves into the model.
"""

from __future__ import annotations

import random

from .models import Track

# Mood presets: a label + the feature thresholds that define the mood. tempo is
# BPM; the rest are 0..1. Thresholds are the *definition*; build_mix scores how
# well each track fits them (soft), so a mood is never an empty/tiny result.
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
# Deliberately broad (esp. indie / alt / electronic / folk) so the ~half of a
# real library that AcousticBrainz misses still has a signal to mix on.
GENRE_MOOD: dict[str, dict] = {
    # High energy / aggressive
    "punk": {"energy": 0.85, "valence": 0.6}, "punk rock": {"energy": 0.85, "valence": 0.6},
    "hardcore punk": {"energy": 0.9, "valence": 0.45}, "post-hardcore": {"energy": 0.85, "valence": 0.4},
    "metal": {"energy": 0.9, "valence": 0.3}, "hard rock": {"energy": 0.8, "valence": 0.5},
    "post-metal": {"energy": 0.7, "valence": 0.3}, "doom metal": {"energy": 0.6, "valence": 0.25},
    "stoner rock": {"energy": 0.7, "valence": 0.45}, "noise rock": {"energy": 0.85, "valence": 0.35},
    "grunge": {"energy": 0.75, "valence": 0.4}, "power pop": {"energy": 0.75, "valence": 0.7},
    "garage rock": {"energy": 0.8, "valence": 0.55}, "garage punk": {"energy": 0.85, "valence": 0.55},
    "pop punk": {"energy": 0.8, "valence": 0.65}, "emo": {"energy": 0.7, "valence": 0.45},
    "midwest emo": {"energy": 0.65, "valence": 0.45}, "dance-punk": {"energy": 0.8, "valence": 0.6},
    "new wave": {"energy": 0.7, "valence": 0.6}, "post-punk": {"energy": 0.68, "valence": 0.42},
    # Rock — medium energy
    "rock": {"energy": 0.65, "valence": 0.5}, "classic rock": {"energy": 0.65, "valence": 0.55},
    "alternative rock": {"energy": 0.6, "valence": 0.45}, "indie rock": {"energy": 0.6, "valence": 0.5},
    "psychedelic rock": {"energy": 0.55, "valence": 0.5}, "neo-psychedelia": {"energy": 0.55, "valence": 0.5},
    "art rock": {"energy": 0.55, "valence": 0.4}, "prog rock": {"energy": 0.6, "valence": 0.45},
    "krautrock": {"energy": 0.55, "valence": 0.45}, "math rock": {"energy": 0.7, "valence": 0.5},
    "post-rock": {"energy": 0.5, "valence": 0.4}, "jangle pop": {"energy": 0.6, "valence": 0.6},
    "britpop": {"energy": 0.65, "valence": 0.55}, "surf rock": {"energy": 0.7, "valence": 0.7},
    "folk rock": {"energy": 0.5, "valence": 0.5}, "indie": {"energy": 0.55, "valence": 0.5},
    "experimental": {"energy": 0.45, "valence": 0.4},
    # Pop — upbeat
    "pop": {"energy": 0.7, "valence": 0.65}, "dance pop": {"energy": 0.8, "valence": 0.7},
    "indie pop": {"energy": 0.6, "valence": 0.6}, "art pop": {"energy": 0.5, "valence": 0.5},
    "baroque pop": {"energy": 0.5, "valence": 0.5}, "chamber pop": {"energy": 0.45, "valence": 0.5},
    "twee pop": {"energy": 0.55, "valence": 0.7}, "synthpop": {"energy": 0.7, "valence": 0.65},
    "synthwave": {"energy": 0.65, "valence": 0.6}, "electropop": {"energy": 0.75, "valence": 0.65},
    "bedroom pop": {"energy": 0.45, "valence": 0.55}, "psychedelic pop": {"energy": 0.6, "valence": 0.6},
    # Folk / acoustic — low energy
    "folk": {"energy": 0.35, "valence": 0.45}, "indie folk": {"energy": 0.35, "valence": 0.5},
    "freak folk": {"energy": 0.4, "valence": 0.5}, "singer-songwriter": {"energy": 0.35, "valence": 0.4},
    "americana": {"energy": 0.4, "valence": 0.45}, "alt country": {"energy": 0.4, "valence": 0.45},
    "alt-country": {"energy": 0.4, "valence": 0.45}, "country": {"energy": 0.5, "valence": 0.55},
    "bluegrass": {"energy": 0.6, "valence": 0.6},
    # Atmospheric / quiet
    "slowcore": {"energy": 0.25, "valence": 0.3}, "sadcore": {"energy": 0.25, "valence": 0.25},
    "dream pop": {"energy": 0.35, "valence": 0.45}, "shoegaze": {"energy": 0.45, "valence": 0.35},
    "ambient": {"energy": 0.2, "valence": 0.4}, "drone": {"energy": 0.25, "valence": 0.35},
    "lo-fi": {"energy": 0.35, "valence": 0.45}, "lullaby": {"energy": 0.15, "valence": 0.5},
    "ethereal wave": {"energy": 0.4, "valence": 0.4}, "darkwave": {"energy": 0.5, "valence": 0.35},
    "goth": {"energy": 0.55, "valence": 0.35},
    # Electronic
    "electronica": {"energy": 0.55, "valence": 0.5}, "idm": {"energy": 0.5, "valence": 0.45},
    "indietronica": {"energy": 0.6, "valence": 0.55}, "techno": {"energy": 0.8, "valence": 0.45},
    "house": {"energy": 0.8, "valence": 0.65}, "deep house": {"energy": 0.65, "valence": 0.55},
    "trip hop": {"energy": 0.4, "valence": 0.35}, "downtempo": {"energy": 0.35, "valence": 0.4},
    # Soul / funk / r&b / hip hop
    "funk": {"energy": 0.75, "valence": 0.7}, "soul": {"energy": 0.55, "valence": 0.55},
    "neo soul": {"energy": 0.5, "valence": 0.55}, "motown": {"energy": 0.65, "valence": 0.7},
    "r&b": {"energy": 0.5, "valence": 0.5}, "disco": {"energy": 0.8, "valence": 0.75},
    "hip hop": {"energy": 0.65, "valence": 0.55}, "rap": {"energy": 0.65, "valence": 0.5},
    "alternative hip hop": {"energy": 0.6, "valence": 0.5},
    # Roots / world / jazz / classical
    "blues": {"energy": 0.5, "valence": 0.4}, "jazz": {"energy": 0.45, "valence": 0.5},
    "vocal jazz": {"energy": 0.4, "valence": 0.5}, "bossa nova": {"energy": 0.4, "valence": 0.6},
    "reggae": {"energy": 0.6, "valence": 0.7}, "ska": {"energy": 0.75, "valence": 0.7},
    "afrobeat": {"energy": 0.7, "valence": 0.7}, "latin": {"energy": 0.7, "valence": 0.7},
    "gospel": {"energy": 0.6, "valence": 0.7}, "classical": {"energy": 0.3, "valence": 0.45},
    "modern classical": {"energy": 0.3, "valence": 0.4}, "soundtrack": {"energy": 0.4, "valence": 0.4},
}

# tempo (BPM) is on a different scale than the 0..1 axes; this is how many BPM of
# miss count as one "unit" of fit penalty, so tempo and 0..1 axes are comparable.
_TEMPO_SCALE = 60.0


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
    the genre estimate, then from derived defaults. Notably, a missing tempo is
    now ESTIMATED from energy (energetic genres → faster) instead of a flat 120,
    so genre-only tracks still differentiate on tempo-based moods. Returns None
    only when the track has neither real features nor a recognized genre.
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
        # Estimate tempo from energy when unknown: ~75 BPM at energy 0 → ~165 at 1.
        "tempo": track.tempo if track.tempo is not None else round(75 + energy * 90, 1),
        "danceability": track.danceability if track.danceability is not None else (energy + valence) / 2,
        "acousticness": track.acousticness if track.acousticness is not None else max(0.0, 1 - energy),
        "speechiness": track.speechiness if track.speechiness is not None else 0.05,
        "instrumentalness": track.instrumentalness if track.instrumentalness is not None else 0.1,
    }


def _criterion_terms(key: str, threshold: float, f: dict) -> tuple[float, float]:
    """For one threshold, return (miss, depth): how far the track VIOLATES it
    (0 if satisfied) and how far it satisfies it BEYOND the threshold (deeper into
    the mood). tempo is rescaled so it's comparable to the 0..1 axes."""
    axis, bound = key.rsplit("_", 1)
    if axis not in f:
        return 0.0, 0.0
    val = f[axis]
    scale = _TEMPO_SCALE if axis == "tempo" else 1.0
    if bound == "max":
        return max(0.0, val - threshold) / scale, max(0.0, threshold - val) / scale
    return max(0.0, threshold - val) / scale, max(0.0, val - threshold) / scale


def mood_fit(features: dict, criteria: dict) -> float:
    """Soft fit score — LOWER is a better fit. 0 means the track satisfies every
    threshold exactly at the line; negative means it sits deeper inside the mood;
    positive means it misses some threshold (by that much). The miss term
    dominates; the depth term only orders tracks that already qualify (a tiny
    tie-break toward the most archetypal)."""
    miss = depth = 0.0
    for key, thr in criteria.items():
        m, d = _criterion_terms(key, thr, features)
        miss += m
        depth += d
    return miss - 0.001 * depth


def track_matches(features: dict, criteria: dict) -> bool:
    """Strict pass: the track satisfies every threshold (no miss on any axis).
    Used to report how many tracks fully fit a mood, distinct from how many the
    soft mix selects."""
    return all(_criterion_terms(key, thr, features)[0] == 0.0 for key, thr in criteria.items())


def build_mix(tracks: list[Track], preset: str | None = None, criteria: dict | None = None,
              limit: int = 30, max_per_artist: int = 1, shuffle_seed: int | None = None,
              exclude_track_ids: set[str] | None = None,
              exclude_artists: set[str] | None = None,
              exclude_genres: set[str] | None = None) -> list[Track]:
    """Score `tracks` against a mood and assemble a varied playlist.

    Pass `preset` (a key in MOOD_PRESETS) or an explicit `criteria` dict. Tracks
    are ranked by FIT (soft scoring), so the result is always the closest `limit`
    tracks — never an empty/tiny strict filter. Variety: skip excluded
    ids/artists/genres (persisted preferences are passed in here), dedup by id, at
    most `max_per_artist` per artist. Deterministic ordering unless `shuffle_seed`
    is given, in which case it shuffles among the best-fitting pool (so you get
    variety without dropping to poor fits).
    """
    if criteria is None:
        if preset not in MOOD_PRESETS:
            raise ValueError(f"unknown mood preset: {preset!r} (known: {', '.join(MOOD_PRESETS)})")
        criteria = MOOD_PRESETS[preset]["criteria"]
    ex_ids = exclude_track_ids or set()
    ex_artists = {a.lower() for a in (exclude_artists or set())}
    ex_genres = {g.lower() for g in (exclude_genres or set())}

    scored: list[tuple[float, Track]] = []
    for t in tracks:
        if t.id in ex_ids or t.artist.lower() in ex_artists:
            continue
        if ex_genres and any(g in ex_genres for g in t.genres):
            continue
        f = resolve_features(t)
        if f is None:
            continue
        scored.append((mood_fit(f, criteria), t))
    # Best fit first; stable tie-break keeps it deterministic.
    scored.sort(key=lambda st: (st[0], st[1].artist.lower(), st[1].name.lower()))

    seen_ids: set[str] = set()
    per_artist: dict[str, int] = {}
    ordered: list[Track] = []
    for _score, t in scored:
        if t.id in seen_ids:
            continue
        artist_key = t.artist.lower()
        if per_artist.get(artist_key, 0) >= max_per_artist:
            continue
        seen_ids.add(t.id)
        per_artist[artist_key] = per_artist.get(artist_key, 0) + 1
        ordered.append(t)

    if shuffle_seed is not None:
        pool = ordered[: max(limit, limit * 3)]
        random.Random(shuffle_seed).shuffle(pool)
        return pool[:limit]
    return ordered[:limit]
