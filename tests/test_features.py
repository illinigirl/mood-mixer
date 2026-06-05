"""Tests for the feature-enrichment layer — the audio-features workaround. HTTP
is stubbed, so these run offline and pin the parsing/derivation math and the
cache behavior (miss rows aren't re-fetched). Uses a sandboxed data dir for the
SQLite cache."""

import pytest

from moodmixer import features


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("MOODMIXER_DATA_DIR", str(tmp_path))


def test_acousticbrainz_parsing_and_derivation(monkeypatch):
    low = {"rhythm": {"bpm": 120, "danceability": 1.5},  # 0-3 scale → expect 0.5
           "lowlevel": {"average_loudness": 0.8}}
    high = {"highlevel": {
        "mood_happy": {"value": "happy", "probability": 0.9},
        "mood_sad": {"value": "not_sad", "probability": 0.8},
        "danceability": {"value": "danceable", "probability": 0.7},
    }}

    def fake_get(url, *args, **kwargs):
        return low if "low-level" in url else high

    monkeypatch.setattr(features, "_http_get_json", fake_get)
    out = features.fetch_acousticbrainz("some-mbid")
    assert out["tempo"] == 120.0
    assert out["danceability"] == pytest.approx(0.5)      # 1.5 / 3
    assert 0.0 <= out["energy"] <= 1.0 and out["energy"] > 0.6   # happy + danceable → energetic
    assert out["valence"] > 0.6                            # happy, not sad → positive
    assert "happy" in out["mood_tags"]


def test_enrich_miss_is_cached_and_not_retried(monkeypatch):
    monkeypatch.setattr(features, "resolve_mbid", lambda *a, **k: None)
    monkeypatch.setattr(features, "fetch_getsongbpm", lambda *a, **k: None)
    assert features.enrich("trk1", "Artist", "Title") is None
    cached = features.get_cached("trk1")
    assert cached["source"] == "miss"
    # get_or_enrich must NOT retry a known miss.
    monkeypatch.setattr(features, "enrich",
                        lambda *a, **k: pytest.fail("should not re-enrich a miss"))
    assert features.get_or_enrich("trk1", "Artist", "Title") is None


def test_enrich_success_caches_features(monkeypatch):
    monkeypatch.setattr(features, "resolve_mbid", lambda *a, **k: "m1")
    monkeypatch.setattr(features, "fetch_acousticbrainz",
                        lambda mbid: {"energy": 0.7, "tempo": 100.0, "source": "acousticbrainz"})
    out = features.enrich("trk2", "Artist", "Title")
    assert out["energy"] == 0.7
    assert features.get_cached("trk2")["source"] == "acousticbrainz"
    assert "trk2" in features.cached_map()
