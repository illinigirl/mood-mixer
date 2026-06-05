"""Spotify I/O — OAuth, reading your liked library, creating playlists.

The other thing (besides features.py) that touches the outside world. Everything
here needs network + your Spotify credentials; the mood engine never does.

Credentials come from the environment so nothing secret lands in the repo:
    MOODMIXER_SPOTIFY_CLIENT_ID, MOODMIXER_SPOTIFY_CLIENT_SECRET
    MOODMIXER_SPOTIFY_REDIRECT_URI   (default http://127.0.0.1:8888/callback)
The OAuth token is cached (and auto-refreshed) in the data dir, gitignored.

Self-hosted, single-user by design: Spotify restricts apps to a manually-managed
allowlist and won't permit public distribution, so each user runs their own app
(see README "Setup"). One-time `authorize()` opens a browser; after that tokens
refresh on their own.
"""

from __future__ import annotations

import base64
import json
import os
import time
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = "user-library-read playlist-modify-private playlist-modify-public"


def _client_id() -> str:
    return os.environ.get("MOODMIXER_SPOTIFY_CLIENT_ID", "")


def _client_secret() -> str:
    return os.environ.get("MOODMIXER_SPOTIFY_CLIENT_SECRET", "")


def _redirect_uri() -> str:
    return os.environ.get("MOODMIXER_SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")


def _token_path() -> Path:
    d = Path(os.environ.get("MOODMIXER_DATA_DIR", Path.home() / ".mood-mixer"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "spotify-token.json"


def credentials_present() -> bool:
    return bool(_client_id() and _client_secret())


def _auth_header() -> str:
    return base64.b64encode(f"{_client_id()}:{_client_secret()}".encode()).decode()


def _save_token(token: dict) -> None:
    token["expires_at"] = time.time() + token.get("expires_in", 3600)
    _token_path().write_text(json.dumps(token))


def _load_token() -> dict | None:
    try:
        return json.loads(_token_path().read_text())
    except FileNotFoundError:
        return None


def _refresh(token: dict) -> dict | None:
    refresh = token.get("refresh_token")
    if not refresh:
        return None
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh},
        headers={"Authorization": f"Basic {_auth_header()}"},
        timeout=15,
    )
    if resp.status_code != 200:
        return None
    new = resp.json()
    new.setdefault("refresh_token", refresh)
    _save_token(new)
    return new


def get_access_token() -> str | None:
    """A valid access token, refreshing if it's within 60s of expiry. None if
    not authorized yet — run authorize()."""
    token = _load_token()
    if not token:
        return None
    if time.time() >= token.get("expires_at", 0) - 60:
        token = _refresh(token)
        if not token:
            return None
    return token.get("access_token")


def authorize() -> bool:
    """One-time OAuth: open a browser, capture the callback on the redirect
    port, exchange the code for tokens. Returns True on success."""
    if not credentials_present():
        raise RuntimeError(
            "Set MOODMIXER_SPOTIFY_CLIENT_ID and MOODMIXER_SPOTIFY_CLIENT_SECRET "
            "(create an app at https://developer.spotify.com/dashboard)."
        )
    auth_code: list[str | None] = [None]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            auth_code[0] = parse_qs(urlparse(self.path).query).get("code", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Done. You can close this tab.</h2></body></html>")

        def log_message(self, *args):
            pass

    url = f"{AUTH_URL}?" + urlencode({
        "client_id": _client_id(), "response_type": "code",
        "redirect_uri": _redirect_uri(), "scope": SCOPES,
    })
    print(f"Opening browser to authorize Spotify. If it doesn't open:\n{url}\n")
    webbrowser.open(url)
    port = int(urlparse(_redirect_uri()).port or 8888)
    server = HTTPServer(("127.0.0.1", port), Handler)
    server.handle_request()
    server.server_close()
    if not auth_code[0]:
        return False
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "authorization_code", "code": auth_code[0],
              "redirect_uri": _redirect_uri()},
        headers={"Authorization": f"Basic {_auth_header()}"},
        timeout=15,
    )
    if resp.status_code != 200:
        return False
    _save_token(resp.json())
    return True


# ---------- reading the library --------------------------------------------

def _get(url: str, headers: dict, params: dict | None = None) -> dict:
    for _attempt in range(3):
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", 2)) + 1)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return {}


def fetch_liked_tracks() -> list[dict]:
    """All liked songs, paginated, each tagged with its primary artist's genres
    (so the mood engine's genre fallback has something to work with). Returns
    raw dicts ready for Track.from_dict / the library cache."""
    token = get_access_token()
    if not token:
        raise RuntimeError("Not authorized — run `mood-mixer authorize` first.")
    headers = {"Authorization": f"Bearer {token}"}

    records: list[dict] = []
    params = {"limit": 50, "offset": 0}
    while True:
        data = _get(f"{API}/me/tracks", headers, params=dict(params))
        items = data.get("items", [])
        if not items:
            break
        for item in items:
            t = item.get("track") or {}
            if not t.get("id") or not t.get("artists"):
                continue
            primary = t["artists"][0]
            records.append({
                "id": t["id"], "name": t.get("name", ""),
                "artist": primary.get("name", ""), "artist_id": primary.get("id", ""),
                "album": (t.get("album") or {}).get("name", ""), "genres": [],
            })
        if len(items) < 50:
            break
        params["offset"] += 50

    _tag_genres(records, headers)
    return records


def _tag_genres(records: list[dict], headers: dict) -> None:
    """Batch-fetch artist genres (50 ids/call) and attach to each record."""
    ids = list(OrderedDict.fromkeys(r["artist_id"] for r in records if r.get("artist_id")))
    genre_map: dict[str, list[str]] = {}
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        try:
            data = _get(f"{API}/artists", headers, params={"ids": ",".join(batch)})
        except requests.RequestException:
            continue
        for artist in data.get("artists", []) or []:
            if artist and artist.get("id"):
                genre_map[artist["id"]] = [g.lower() for g in artist.get("genres", [])]
    for r in records:
        r["genres"] = genre_map.get(r.get("artist_id", ""), [])


# ---------- creating a playlist --------------------------------------------

def create_playlist(name: str, uris: list[str], description: str = "", public: bool = False) -> dict:
    """Create a private playlist and add the given track URIs (chunked at
    Spotify's 100-per-call limit). Returns {playlist_id, playlist_url,
    track_count}."""
    token = get_access_token()
    if not token:
        raise RuntimeError("Not authorized — run `mood-mixer authorize` first.")
    if not uris:
        raise ValueError("no tracks to add")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    me = requests.get(f"{API}/me", headers=headers, timeout=15)
    me.raise_for_status()
    user_id = me.json()["id"]

    created = requests.post(
        f"{API}/users/{user_id}/playlists", headers=headers,
        json={"name": name[:100], "description": description[:300], "public": public},
        timeout=15,
    )
    created.raise_for_status()
    playlist = created.json()
    pid = playlist["id"]

    for start in range(0, len(uris), 100):
        add = requests.post(
            f"{API}/playlists/{pid}/tracks", headers=headers,
            json={"uris": uris[start:start + 100]}, timeout=15,
        )
        add.raise_for_status()

    return {
        "playlist_id": pid,
        "playlist_url": playlist.get("external_urls", {}).get("spotify", ""),
        "track_count": len(uris),
    }


def update_playlist_details(playlist_id: str, name: str | None = None,
                            description: str | None = None) -> None:
    """Change an existing playlist's name and/or description (the change-details
    endpoint). No-op if neither is given."""
    body: dict = {}
    if name:
        body["name"] = name[:100]
    if description is not None:
        body["description"] = description[:300]
    if not body:
        return
    token = get_access_token()
    if not token:
        raise RuntimeError("Not authorized — run `mood-mixer authorize` first.")
    resp = requests.put(f"{API}/playlists/{playlist_id}",
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                        json=body, timeout=15)
    resp.raise_for_status()


def replace_playlist_tracks(playlist_id: str, uris: list[str]) -> dict:
    """Replace ALL tracks in an existing playlist (rebuild in place — keeps its
    name/URL, swaps the contents). PUT replaces with the first 100, then POST
    appends the rest in 100-track chunks."""
    token = get_access_token()
    if not token:
        raise RuntimeError("Not authorized — run `mood-mixer authorize` first.")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    put = requests.put(f"{API}/playlists/{playlist_id}/tracks", headers=headers,
                       json={"uris": uris[:100]}, timeout=15)
    put.raise_for_status()
    for start in range(100, len(uris), 100):
        add = requests.post(f"{API}/playlists/{playlist_id}/tracks", headers=headers,
                            json={"uris": uris[start:start + 100]}, timeout=15)
        add.raise_for_status()
    return {"playlist_id": playlist_id, "track_count": len(uris)}
