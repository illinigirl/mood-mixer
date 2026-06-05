"""Unit tests for the pure mood engine — no I/O, no Spotify, stdlib only. This is
where the bulk of the logic is pinned: the genre fallback, the threshold filter,
and the variety rules in build_mix."""

import pytest

from moodmixer import moods
from moodmixer.models import Track


def test_seven_presets_exposed():
    presets = moods.list_presets()
    keys = {p["key"] for p in presets}
    assert keys == {"chill", "morning", "focus", "roadtrip", "workout", "melancholy", "party"}


def test_genre_estimate_known_unknown_and_average():
    assert moods.estimate_mood_from_genres([]) is None
    assert moods.estimate_mood_from_genres(["zolo-core"]) is None  # unknown genre
    folk = moods.estimate_mood_from_genres(["indie folk"])
    assert folk == pytest.approx({"energy": 0.35, "valence": 0.5})
    avg = moods.estimate_mood_from_genres(["indie folk", "punk"])  # (.35+.85)/2, (.5+.6)/2
    assert avg["energy"] == pytest.approx(0.6)
    assert avg["valence"] == pytest.approx(0.55)


def test_resolve_features_fills_genre_only_track():
    # No audio features, but a known genre → engine fills from the estimate.
    t = Track(id="x", name="n", artist="a", genres=["indie folk"])
    f = moods.resolve_features(t)
    assert f["energy"] == pytest.approx(0.35)
    assert f["tempo"] == 120.0  # neutral default
    # No features AND no recognized genre → nothing to go on → skipped.
    assert moods.resolve_features(Track(id="y", name="n", artist="a", genres=["zolo-core"])) is None


def test_workout_matches_are_high_energy_and_fast(library):
    mix = moods.build_mix(library, "workout")
    assert len(mix) == 3
    for t in mix:
        assert t.energy >= 0.75 and t.tempo >= 120


def test_melancholy_is_low_energy_low_valence(library):
    mix = moods.build_mix(library, "melancholy")
    assert [t.name for t in mix] == ["Grey Morning"]


def test_one_track_per_artist_then_relaxed(library):
    # chill matches 5 entries but two of them share the artist "Marrow".
    capped = moods.build_mix(library, "chill", max_per_artist=1)
    relaxed = moods.build_mix(library, "chill", max_per_artist=2)
    assert len(capped) == 4
    assert len(relaxed) == 5
    artists = [t.artist for t in capped]
    assert len(artists) == len(set(artists))  # no artist twice when capped


def test_roadtrip_caps_repeated_artist(library):
    mix = moods.build_mix(library, "roadtrip", max_per_artist=1)
    artists = [t.artist for t in mix]
    assert artists.count("The Volters") == 1


def test_focus_prefers_instrumental(library):
    names = {t.name for t in moods.build_mix(library, "focus")}
    assert {"Quiet Study", "Glass Fields"} <= names


def test_limit_is_respected(library):
    assert len(moods.build_mix(library, "roadtrip", limit=2, max_per_artist=2)) == 2


def test_deterministic_without_seed_reproducible_with_seed(library):
    a = moods.build_mix(library, "workout")
    b = moods.build_mix(library, "workout")
    assert [t.id for t in a] == [t.id for t in b]
    s1 = moods.build_mix(library, "workout", shuffle_seed=7)
    s2 = moods.build_mix(library, "workout", shuffle_seed=7)
    assert [t.id for t in s1] == [t.id for t in s2]


def test_unknown_preset_raises(library):
    with pytest.raises(ValueError, match="unknown mood preset"):
        moods.build_mix(library, "nonsense")


def test_explicit_criteria_bypasses_presets(library):
    mix = moods.build_mix(library, criteria={"tempo_min": 140})
    assert all(t.tempo >= 140 for t in mix)
    assert mix  # Neon Sprint (140) + Midnight Run (150)
