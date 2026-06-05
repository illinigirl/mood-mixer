"""Integration tests for the MCP tool layer — the surface a reviewer drives.
Tools are plain functions, called in-process against a sandboxed data dir (so
they read the bundled sample library + a fresh preferences file). Spotify is
monkeypatched, so nothing hits the network. Skips if the MCP SDK isn't installed.
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
    assert st["real_features"] == 10
    assert st["genre_estimated"] == 1
    assert st["no_features"] == 1
    assert st["spotify_authorized"] is False


def test_preview_reports_strict_and_selected(srv):
    res = srv.preview_mix("workout", limit=3)
    assert res["strict_matches"] == 3      # 3 tracks fully fit workout
    assert res["selected"] == 3
    assert all(t["features_source"] for t in res["tracks"])


def test_preview_soft_fills_a_strict_tiny_mood(srv):
    # Only 1 strict melancholy match, but soft scoring returns the closest N.
    res = srv.preview_mix("melancholy", limit=4)
    assert res["strict_matches"] == 1
    assert res["selected"] == 4
    assert res["tracks"][0]["name"] == "Grey Morning"


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
    res = srv.create_playlist("workout", name="Sweat", limit=3)
    assert res["created"] is True
    assert res["track_count"] == 3
    assert captured["name"] == "Sweat"
    assert all(u.startswith("spotify:track:") for u in captured["uris"])


def test_create_playlist_unauthorized_errors(srv):
    res = srv.create_playlist("workout", limit=3)
    assert "error" in res and "authorize" in res["error"].lower()


def test_rebuild_replaces_in_place(srv, monkeypatch):
    seen = {}

    def fake_replace(playlist_id, uris):
        seen["id"] = playlist_id
        seen["uris"] = uris
        return {"playlist_id": playlist_id, "track_count": len(uris)}

    monkeypatch.setattr("moodmixer.spotify.replace_playlist_tracks", fake_replace)
    res = srv.create_playlist("workout", limit=3, replace_playlist_id="plX")
    assert res["updated"] is True
    assert res["playlist_url"].endswith("plX")
    assert seen["id"] == "plX" and len(seen["uris"]) == 3


def test_refresh_library_caches_then_status_flips(srv, monkeypatch):
    fake = [{"id": "abc1234567890123456789", "name": "X", "artist": "Y", "genres": ["pop"]}]
    monkeypatch.setattr("moodmixer.spotify.fetch_liked_tracks", lambda: fake)
    out = srv.refresh_library()
    assert out["refreshed"] is True and out["track_count"] == 1
    assert srv.get_library_status()["source"] == "cache"


def test_enrich_features_batches(srv, monkeypatch):
    monkeypatch.setattr("moodmixer.features.enrich", lambda *a, **k: None)
    out = srv.enrich_features(limit=3)
    assert out["attempted"] == 3
    assert out["enriched"] == 0


def test_saved_artist_exclusion_persists_and_overrides(srv):
    base = srv.preview_mix("workout", limit=10)["strict_matches"]
    assert base == 3
    # "skip The Volters from now on"
    srv.add_exclusion(artists=["The Volters"], note="no Volters")
    assert "the volters" in srv.list_preferences()["excluded_artists"]
    after = srv.preview_mix("workout", limit=10)
    assert all(t["artist"] != "The Volters" for t in after["tracks"])
    assert after["strict_matches"] == 2                     # one workout fit was The Volters
    # "...unless I ask" — override just this request
    allowed = srv.preview_mix("workout", limit=10, allow_artists=["The Volters"])
    assert any(t["artist"] == "The Volters" for t in allowed["tracks"])
    # undo the standing rule
    srv.remove_exclusion(artists=["The Volters"])
    assert "the volters" not in srv.list_preferences()["excluded_artists"]


def test_saved_genre_exclusion_applies(srv):
    srv.add_exclusion(genres=["indie folk"], note="no lullabies-adjacent folk")
    res = srv.preview_mix("chill", limit=20)
    assert all(t["artist"] != "Cedar and Salt" for t in res["tracks"])  # the indie-folk track
