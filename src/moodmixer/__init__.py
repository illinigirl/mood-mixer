"""mood-mixer — a self-contained MCP server that builds mood-based Spotify
playlists from your liked library.

Spotify deprecated its audio-features endpoint (Nov 2024), so mood-mixer sources
energy/valence/tempo from community data (AcousticBrainz / GetSongBPM), matches
your library against tunable mood presets with deterministic math, and creates a
real Spotify playlist. Pure mood engine + thin adapters; the LLM narrates, the
data plane computes."""

__version__ = "0.1.0"
