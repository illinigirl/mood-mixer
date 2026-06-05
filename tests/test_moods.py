"""Unit tests for the pure mood engine — no I/O, no Spotify, stdlib only. Covers
the genre fallback (now richer), soft fit scoring, exclusions, and the variety
rules in build_mix."""

import pytest

from moodmixer import moods
from moodmixer.models import Track


def test_seven_presets_exposed():
    keys = {p["key"] for p in moods.list_presets()}
    assert keys == {"chill", "morning", "focus", "roadtrip", "workout", "melancholy", "party"}


def test_genre_estimate_known_unknown_and_average():
    assert moods.estimate_mood_from_genres([]) is None
    assert moods.estimate_mood_from_genres(["zolo-core"]) is None  # unknown genre
    folk = moods.estimate_mood_from_genres(["indie folk"])
    assert folk == pytest.approx({"energy": 0.35, "valence": 0.5})
    avg = moods.estimate_mood_from_genres(["indie folk", "punk"])  # (.35+.85)/2, (.5+.6)/2
    assert avg["energy"] == pytest.approx(0.6)
    assert avg["valence"] == pytest.approx(0.55)


def test_richer_fallback_resolves_more_genres():
    # Genres added in the richer table now resolve instead of being dropped.
    for g in ("post-punk", "trip hop", "shoegaze", "bossa nova"):
        assert moods.resolve_features(Track(id="x", name="n", artist="a", genres=[g])) is not None
    # Truly unknown genre with no features still can't be placed.
    assert moods.resolve_features(Track(id="y", name="n", artist="a", genres=["zolo-core"])) is None


def test_tempo_estimated_from_energy_for_genre_only_tracks():
    fast = moods.resolve_features(Track(id="a", name="n", artist="a", genres=["techno"]))    # energy .8
    slow = moods.resolve_features(Track(id="b", name="n", artist="a", genres=["ambient"]))   # energy .2
    assert fast["tempo"] > 120          # energetic genre → faster than the old flat 120
    assert slow["tempo"] < 120          # calm genre → slower
    # explicit: 75 + energy*90
    assert fast["tempo"] == pytest.approx(75 + 0.8 * 90)


def test_soft_scoring_returns_full_playlist_not_tiny_filter(library):
    # Only ONE track strictly matches melancholy, but soft scoring still returns a
    # full playlist (the closest N), with the strict match ranked first.
    crit = moods.MOOD_PRESETS["melancholy"]["criteria"]
    strict = [t for t in library if (f := moods.resolve_features(t)) and moods.track_matches(f, crit)]
    assert len(strict) == 1 and strict[0].name == "Grey Morning"
    mix = moods.build_mix(library, "melancholy", limit=4)
    assert mix[0].name == "Grey Morning"     # strict match leads
    assert len(mix) == 4                       # ...and the closest others fill it out


def test_workout_top_fits_are_high_energy_and_fast(library):
    mix = moods.build_mix(library, "workout", limit=3)
    assert len(mix) == 3
    for t in mix:
        assert t.energy >= 0.75 and t.tempo >= 120


def test_one_track_per_artist_then_relaxed(library):
    cap1 = moods.build_mix(library, "chill", limit=50, max_per_artist=1)
    cap2 = moods.build_mix(library, "chill", limit=50, max_per_artist=2)
    assert len(set(t.artist for t in cap1)) == len(cap1)           # no artist twice
    assert [t.artist for t in cap1].count("Marrow") == 1
    assert [t.artist for t in cap2].count("Marrow") == 2           # both Marrow tracks fit chill
    assert len(cap2) > len(cap1)


def test_focus_prefers_instrumental(library):
    names = {t.name for t in moods.build_mix(library, "focus", limit=4)}
    assert {"Quiet Study", "Glass Fields"} <= names


def test_exclusions_drop_tracks(library):
    by_artist = moods.build_mix(library, "workout", limit=50, exclude_artists={"the volters"})
    assert all(t.artist != "The Volters" for t in by_artist)
    by_genre = moods.build_mix(library, "chill", limit=50, exclude_genres={"indie folk"})
    assert all("indie folk" not in t.genres for t in by_genre)
    excluded_id = "sample0000000000000009"
    by_id = moods.build_mix(library, "party", limit=50, exclude_track_ids={excluded_id})
    assert all(t.id != excluded_id for t in by_id)


def test_limit_is_respected(library):
    assert len(moods.build_mix(library, "roadtrip", limit=2)) == 2


def test_deterministic_without_seed_reproducible_with_seed(library):
    a = moods.build_mix(library, "workout", limit=3)
    b = moods.build_mix(library, "workout", limit=3)
    assert [t.id for t in a] == [t.id for t in b]
    s1 = moods.build_mix(library, "chill", limit=5, shuffle_seed=7)
    s2 = moods.build_mix(library, "chill", limit=5, shuffle_seed=7)
    assert [t.id for t in s1] == [t.id for t in s2]


def test_unknown_preset_raises(library):
    with pytest.raises(ValueError, match="unknown mood preset"):
        moods.build_mix(library, "nonsense")


def test_explicit_criteria_bypasses_presets(library):
    mix = moods.build_mix(library, criteria={"tempo_min": 140}, limit=2)
    assert all(t.tempo >= 140 for t in mix)   # the two fastest tracks
