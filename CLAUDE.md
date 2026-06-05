# CLAUDE.md — mood-mixer guidance

Read this first. It's the contract for working in this repo with an agent: what
the pieces are, how to run and test, the design decisions, and the test for
whether a new tool belongs here.

## What this is

A self-contained MCP server that builds mood-based Spotify playlists from your
liked library. A deterministic data plane — match your tracks to a mood, create
a real playlist — that the LLM drives and narrates.

The point of interest: **Spotify deprecated `/audio-features` in Nov 2024**, so a
new app can't read energy/valence/tempo. mood-mixer rebuilds that signal from
community sources (AcousticBrainz + GetSongBPM), caches it, and matches with
exact thresholds. That workaround is the heart of the project.

## Why this is an MCP — the four-part test

A tool earns its place only if it does something the model can't. Four such
things; this hits all four:

| # | Need | Plain Claude | mood-mixer |
|---|---|---|---|
| 1 | Persist state across sessions | ❌ forgets | ✅ library cache + features DB |
| 2 | Side effect / durable artifact | ❌ can't act | ✅ `create_playlist` (real Spotify playlist) |
| 3 | Private / live data | ❌ blind to it | ✅ your liked library + genres |
| 4 | Compute exactly | ❌ hand-waves | ✅ threshold filter over real features |

When adding or changing a tool, ask which of the four it serves. If "none,"
it probably shouldn't be a tool.

## Architecture: pure engine + thin adapters

| Module | Role | I/O? |
|---|---|---|
| `models.py` | `Track` (identity + audio features) | no |
| `moods.py` | presets, genre fallback, threshold filter, `build_mix` | no |
| `features.py` | AcousticBrainz/GetSongBPM enrichment + SQLite cache | **yes** |
| `spotify.py` | OAuth, read liked library, create playlist | **yes** |
| `store.py` | library cache + hydrating it with cached features | **yes** |
| `server.py` | MCP adapter (FastMCP), dual stdio/HTTP transport | via the above |
| `cli.py` | CLI adapter; hosts the one-time `authorize` | via the above |

Keep matching logic in `moods.py` and test it directly; adapters stay thin
(parse args → call the engine → persist/act → return a dict).

## Run + test

```bash
pip install -e ".[test]"
python -m pytest -q                 # 27 tests, NO network (Spotify + feature APIs stubbed)
ruff check .

PYTHONPATH=src python -m moodmixer.cli moods
PYTHONPATH=src python -m moodmixer.cli preview chill     # works cold on the sample
```

Setup for live use (Spotify app + auth + library) is in README "Setup". Data
lives in `MOODMIXER_DATA_DIR` (default `~/.mood-mixer`); point it at a temp dir
to experiment.

## Design decisions

- **Deterministic math in the data plane; the LLM narrates.** `build_mix` and the
  threshold filter are exact. The model chooses the mood and talks about the
  result — it never decides whether a track is "energetic enough."
- **Audio features sourced + cached, never guessed.** Spotify's deprecation is
  routed around with AcousticBrainz (energy/valence derived from its mood
  classifiers; tempo/danceability direct) → GetSongBPM (BPM) → a genre estimate
  so nothing is dropped. Misses are cached too, so we never re-fetch.
- **Bundled sample vs. mutable state, kept apart.** `data/sample-library.json`
  ships read-only so the repo (and the read/preview path + tests) runs cold; your
  liked-library cache, features DB, and OAuth token live in a gitignored data dir.
- **Self-hosted, single-user — by Spotify's rules, not ours.** Spotify restricts
  apps to a manual allowlist and forbids public distribution, so each user runs
  their own app. Credentials come from env vars; nothing secret is in the repo.
- **Dual transport from one codebase.** stdio (Claude Desktop) or `--http` /
  `MOOD_MIXER_HTTP=1` (connector). `_resolve_transport` is factored out so it's
  testable without binding a port.
- **Tool-layer integration tests, not just engine unit tests.** `test_moods.py`
  covers the engine; `test_server_tools.py` drives the tools in-process with
  Spotify monkeypatched; `test_features.py` pins the enrichment math offline.

## A data-shape assumption to retire before it bites

`load_library` reads the whole library into memory and `build_mix` scans it. Fine
for a personal library (thousands of tracks). If it ever needs to scale to a huge
catalog, this gets slow with no code change — index or stream, and write the
assumption down as an expiring contract.

## Deliberate simplifications (each a good first task)

These are honest limitations and the natural places to extend:

1. **Library = your liked songs only.** `spotify.fetch_liked_tracks` could also
   pull saved albums + owned playlists (the dashboard this came from does).
2. **No discovery.** v1 mixes only tracks you already like. Adding similar-track
   candidates (ListenBrainz/Deezer) beyond your library is the headline
   extension — keep it seedable from named artists so it's useful with a thin
   library.
3. **No recent-play avoidance.** A play-history store could keep a mix from
   repeating what you just heard. Personalization; deferred on purpose.
4. **Presets are hardcoded thresholds.** Make them user-editable, or let a custom
   `criteria` dict (already supported by `build_mix`) be exposed as a tool.
5. **Genre→mood table is coarse.** Extend `GENRE_MOOD`, or add more feature
   sources (e.g. a bundled features dataset) as another tier.
6. **Variety is one-per-artist + dedup.** Add diversity-by-genre or a target
   tempo curve if mixes feel samey.

## Things to keep true

- Don't put secrets in the repo — credentials come from env vars; the token,
  cache, and DB are gitignored.
- Keep the bundled sample generic and claim-free (fabricated artists/tracks) —
  it ships in a public repo.
- The feature-enrichment APIs are rate-limited; `enrich_features` is intentionally
  batched and slow. Don't remove the throttle.
