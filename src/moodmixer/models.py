"""Domain models for mood-mixer.

Plain dataclasses, no I/O. The mood engine (moods.py) reasons entirely over
these, so it's unit-tested without Spotify, the network, or a database.

A Track carries both its identity (id/name/artist/uri) and its audio features
(energy/valence/tempo/...). Features are `None` when unknown — Spotify deprecated
its audio-features endpoint in Nov 2024, so we source them from community data
(see features.py) or fall back to a genre estimate (see moods.py). Keeping the
unknowns explicit is what lets the engine decide when to estimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The audio-feature axes the mood filter reasons over. tempo is BPM; the rest
# are 0..1 (matching the scale of Spotify's old audio-features endpoint).
FEATURE_AXES = (
    "energy", "valence", "tempo", "danceability",
    "acousticness", "speechiness", "instrumentalness",
)


@dataclass(frozen=True)
class Track:
    """One track from the user's library, optionally enriched with audio
    features. `genres` are the artist's genres (used for the fallback mood
    estimate when features are missing)."""

    id: str
    name: str
    artist: str
    album: str = ""
    genres: list[str] = field(default_factory=list)
    # Audio features — None when unknown (see features.py / the genre fallback).
    energy: float | None = None
    valence: float | None = None
    tempo: float | None = None
    danceability: float | None = None
    acousticness: float | None = None
    speechiness: float | None = None
    instrumentalness: float | None = None
    features_source: str | None = None  # 'acousticbrainz' | 'getsongbpm' | 'genre' | None

    @property
    def uri(self) -> str:
        """The Spotify track URI used when creating a playlist."""
        return f"spotify:track:{self.id}"

    @classmethod
    def from_dict(cls, d: dict) -> Track:
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            artist=d.get("artist", ""),
            album=d.get("album", ""),
            genres=[g.lower() for g in d.get("genres", [])],
            energy=d.get("energy"),
            valence=d.get("valence"),
            tempo=d.get("tempo"),
            danceability=d.get("danceability"),
            acousticness=d.get("acousticness"),
            speechiness=d.get("speechiness"),
            instrumentalness=d.get("instrumentalness"),
            features_source=d.get("features_source"),
        )
