"""Smoke test: the MCP server imports and registers its tools, and transport
selection works. Skips cleanly if the `mcp` SDK isn't installed."""

import pytest

pytest.importorskip("mcp")


def test_server_exposes_expected_tools():
    import moodmixer.server as server

    expected = {
        "list_moods", "get_library_status", "preview_mix",
        "create_playlist", "refresh_library", "enrich_features",
        "add_exclusion", "list_preferences", "remove_exclusion",
    }
    missing = {name for name in expected if not hasattr(server, name)}
    assert not missing, f"server missing tools: {missing}"


def test_transport_defaults_to_stdio():
    from moodmixer.server import _resolve_transport

    assert _resolve_transport([])[0] == "stdio"


def test_http_flag_selects_streamable_http():
    from moodmixer.server import _resolve_transport

    transport, host, port = _resolve_transport(["--http", "--port", "8765"])
    assert transport == "streamable-http"
    assert port == 8765


def test_http_env_var_selects_streamable_http(monkeypatch):
    from moodmixer.server import _resolve_transport

    monkeypatch.setenv("MOOD_MIXER_HTTP", "1")
    assert _resolve_transport([])[0] == "streamable-http"
