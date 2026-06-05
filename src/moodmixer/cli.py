"""A thin CLI over the same calls the MCP tools make — so the project runs and
demos without an MCP client, and so the one-time Spotify auth (a browser flow)
has a home outside the tool surface.

    python -m moodmixer.cli moods
    python -m moodmixer.cli status
    python -m moodmixer.cli authorize                 # one-time Spotify OAuth
    python -m moodmixer.cli refresh                    # pull liked songs → cache
    python -m moodmixer.cli enrich [--limit 50]        # backfill audio features
    python -m moodmixer.cli preview <mood> [--limit 30]
    python -m moodmixer.cli create  <mood> [--name "..."] [--limit 30]
"""

from __future__ import annotations

import argparse

from . import features, moods, spotify, store


def cmd_moods(args):
    for p in moods.list_presets():
        crit = ", ".join(f"{k}={v}" for k, v in p["criteria"].items())
        print(f"  {p['key']:<11} {p['label']:<16} [{crit}]")


def cmd_status(args):
    lib = store.load_library()
    real = sum(1 for t in lib if t.features_source in {"acousticbrainz", "getsongbpm", "mixed", "sample"})
    print(f"  tracks:   {len(lib)} (source: {store.library_source()})")
    print(f"  features: {real} real, {len(lib) - real} estimated/none")
    print(f"  spotify:  {'authorized' if spotify.get_access_token() else 'NOT authorized — run authorize'}")


def cmd_authorize(args):
    ok = spotify.authorize()
    print("Authorized." if ok else "Authorization failed.")


def cmd_refresh(args):
    records = spotify.fetch_liked_tracks()
    n = store.save_library(records)
    print(f"Cached {n} liked tracks. Run `enrich` next to add audio features.")


def cmd_enrich(args):
    features.init_schema()
    raw = store.load_library(hydrate=False)
    todo = [t for t in raw if features.get_cached(t.id) is None][:args.limit]
    hit = 0
    for t in todo:
        if features.enrich(t.id, t.artist, t.name):
            hit += 1
        print(f"  {t.artist} — {t.name}: {'ok' if features.get_cached(t.id) and features.get_cached(t.id)['source'] != 'miss' else 'miss'}")
    print(f"Enriched {hit}/{len(todo)}.")


def _saved_excludes():
    p = store.load_preferences()
    return (set(p["excluded_track_ids"]),
            {a.lower() for a in p["excluded_artists"]},
            {g.lower() for g in p["excluded_genres"]})


def cmd_preview(args):
    ids, artists, genres = _saved_excludes()
    mix = moods.build_mix(store.load_library(), args.mood, limit=args.limit, shuffle_seed=args.seed,
                          exclude_track_ids=ids, exclude_artists=artists, exclude_genres=genres)
    print(f"{len(mix)} tracks for '{args.mood}':\n")
    for t in mix:
        print(f"  {t.artist:<24} {t.name:<28} [{t.features_source or 'genre'}]")


def cmd_create(args):
    ids, artists, genres = _saved_excludes()
    mix = moods.build_mix(store.load_library(), args.mood, limit=args.limit, shuffle_seed=args.seed,
                          exclude_track_ids=ids, exclude_artists=artists, exclude_genres=genres)
    if not mix:
        print(f"No tracks available for '{args.mood}' (after exclusions).")
        return
    label = moods.MOOD_PRESETS[args.mood]["label"]
    result = spotify.create_playlist(
        args.name or f"{label} (mood-mixer)", [t.uri for t in mix],
        description=f"Built by mood-mixer — mood: {label}.",
    )
    print(f"Created '{args.name or label}' ({result['track_count']} tracks): {result['playlist_url']}")


def cmd_exclude(args):
    prefs = store.add_exclusion(artists=args.artist, genres=args.genre,
                                track_ids=args.track, note=args.note)
    print(f"Saved. Excluding {len(prefs['excluded_artists'])} artists, "
          f"{len(prefs['excluded_genres'])} genres, {len(prefs['excluded_track_ids'])} tracks.")


def cmd_prefs(args):
    p = store.load_preferences()
    print("Excluded artists:", ", ".join(p["excluded_artists"]) or "(none)")
    print("Excluded genres: ", ", ".join(p["excluded_genres"]) or "(none)")
    print("Excluded tracks: ", len(p["excluded_track_ids"]))
    for n in p["notes"]:
        print("  note:", n)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="moodmixer", description="mood-mixer CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("moods", help="list mood presets").set_defaults(func=cmd_moods)
    sub.add_parser("status", help="library + feature + auth status").set_defaults(func=cmd_status)
    sub.add_parser("authorize", help="one-time Spotify OAuth").set_defaults(func=cmd_authorize)
    sub.add_parser("refresh", help="pull liked songs into the cache").set_defaults(func=cmd_refresh)

    pe = sub.add_parser("enrich", help="backfill audio features")
    pe.add_argument("--limit", type=int, default=50)
    pe.set_defaults(func=cmd_enrich)

    pp = sub.add_parser("preview", help="show what a mood would select")
    pp.add_argument("mood")
    pp.add_argument("--limit", type=int, default=30)
    pp.add_argument("--seed", type=int, default=None)
    pp.set_defaults(func=cmd_preview)

    pc = sub.add_parser("create", help="create a real Spotify playlist")
    pc.add_argument("mood")
    pc.add_argument("--name", default=None)
    pc.add_argument("--limit", type=int, default=30)
    pc.add_argument("--seed", type=int, default=None)
    pc.set_defaults(func=cmd_create)

    px = sub.add_parser("exclude", help="save a 'skip from now on' rule")
    px.add_argument("--artist", action="append", help="repeatable")
    px.add_argument("--genre", action="append", help="repeatable")
    px.add_argument("--track", action="append", help="track id, repeatable")
    px.add_argument("--note", default=None)
    px.set_defaults(func=cmd_exclude)

    sub.add_parser("prefs", help="show saved exclusions").set_defaults(func=cmd_prefs)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
