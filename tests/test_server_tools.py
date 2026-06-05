"""Integration tests for the MCP tool layer — the surface a reviewer drives.
Tools are plain functions, called in-process against a sandboxed data dir (so
they read the bundled sample library, not your real one). Spotify is monkey-
patched, so nothing hits the network. Skips if the MCP SDK isn't installed.
"""

import pytest

pytest.importorskip("mcp")


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("MOODMIXER_DATA_DIR", str(tmp_path))
    import moodmixer.server as server
    return server


def test_list_moods(srv):
    keys = {m["key"] for m in srv.list_moods()["moods"]}
    assert keys == {"chill", "morning", "focus", "roadtrip", "workout", "melancholy", "party"}


def test_library_status_reads_sample(srv):
    st = srv.get_library_status()
    assert st["track_count"] == 12
    assert st["source"] == "sample"
    assert st["real_features"] == 10        # 10 sample tracks carry features
    assert st["genre_estimated"] == 1       # the genre-only indie-folk track
    assert st["no_features"] == 1           # the unknown-genre track
    assert st["spotify_authorized"] is False


def test_preview_mix_grounds_without_side_effect(srv):
    res = srv.preview_mix("workout")
    assert res["selected"] == 3
    assert res["matched"] == 3
    assert all(t["features_source"] for t in res["tracks"])


def test_preview_unknown_mood_errors(srv):
    assert "error" in srv.preview_mix("nonsense")


def test_create_playlist_calls_spotify(srv, monkeypatch):
    captured = {}

    def fake_create(name, uris, description="", public=False):
        captured["name"] = name
        captured["uris"] = uris
        return {"playlist_id": "pl1", "playlist_url": "https://open.spotify.com/playlist/pl1",
                "track_count": len(uris)}

    monkeypatch.setattr("moodmixer.spotify.create_playlist", fake_create)
    res = srv.create_playlist("workout", name="Sweat")
    assert res["created"] is True
    assert res["playlist_url"].endswith("pl1")
    assert res["track_count"] == 3
    assert captured["name"] == "Sweat"
    assert all(u.startswith("spotify:track:") for u in captured["uris"])


def test_create_playlist_unauthorized_errors(srv):
    # No token + no monkeypatch → spotify.create_playlist raises → tool returns error.
    res = srv.create_playlist("workout")
    assert "error" in res and "authorize" in res["error"].lower()


def test_refresh_library_caches_then_status_flips(srv, monkeypatch):
    fake = [{"id": "abc1234567890123456789", "name": "X", "artist": "Y", "genres": ["pop"]}]
    monkeypatch.setattr("moodmixer.spotify.fetch_liked_tracks", lambda: fake)
    out = srv.refresh_library()
    assert out["refreshed"] is True and out["track_count"] == 1
    assert srv.get_library_status()["source"] == "cache"


def test_enrich_features_batches(srv, monkeypatch):
    # Don't hit the network: pretend every lookup misses.
    monkeypatch.setattr("moodmixer.features.enrich", lambda *a, **k: None)
    out = srv.enrich_features(limit=3)
    assert out["attempted"] == 3
    assert out["enriched"] == 0
