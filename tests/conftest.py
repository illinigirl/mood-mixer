"""Test fixtures + import-path setup.

Puts `src/` on the path so tests can `from moodmixer import ...` without an
editable install — the pure mood-engine tests run on stdlib alone. Only the
tool-layer tests need the MCP SDK (and they monkeypatch Spotify, so no network).
"""

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

from moodmixer.models import Track  # noqa: E402

_SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample-library.json"


@pytest.fixture
def library() -> list[Track]:
    """The bundled sample library, as Track objects."""
    raw = json.loads(_SAMPLE.read_text())
    return [Track.from_dict(t) for t in raw["tracks"]]
