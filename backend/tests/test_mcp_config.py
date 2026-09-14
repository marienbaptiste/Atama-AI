"""The generated mcp.json (spec §11, ADR-039): one server, search, and only when SearxNG is
configured; no SRS server, no credential anywhere in it, and the allowed tools match."""
from __future__ import annotations

import json

from backend import config
from backend.tools import mcp_config

WK, BP = "wk-live-secret-1234567890", "bp-live-secret-0987654321"


def cfg(tmp_path, **values):
    settings = tmp_path / "settings.json"
    stored = {"CACHE_DIR": str(tmp_path / ".cache"), "WANIKANI_TOKEN": WK, "BUNPRO_API_TOKEN": BP, **values}
    settings.write_text(json.dumps({k.lower(): v for k, v in stored.items()}), encoding="utf-8")
    loaded = config.load(settings, env={})
    assert (loaded.WANIKANI_TOKEN, loaded.BUNPRO_API_TOKEN) == (WK, BP)   # or the checks below prove nothing
    return loaded


def test_search_is_the_only_server_and_it_holds_no_credential(tmp_path):
    c = cfg(tmp_path, SEARXNG_URL="http://127.0.0.1:8888")
    built = mcp_config.build(c)
    assert list(built["mcpServers"]) == ["search"]
    assert built["mcpServers"]["search"]["args"] == ["-m", "backend.search_mcp"]
    text = json.dumps(built)
    assert WK not in text and BP not in text and "bunpro" not in text.lower() and "wanikani" not in text.lower()
    assert mcp_config.allowed_tools(c) == ("mcp__search__search",)
    assert list(mcp_config.markers(c)) == ["search"]
    assert mcp_config.markers(c)["search"] == mcp_config.ready_marker(c)


def test_an_srs_token_alone_configures_no_server_and_no_tool(tmp_path):
    """Before ADR-039 a Bunpro token added the Bunpro server; now the tokens reach only the fetch."""
    c = cfg(tmp_path, SEARXNG_URL="")
    assert mcp_config.build(c) == {"mcpServers": {}}
    assert mcp_config.allowed_tools(c) == ()
    assert mcp_config.markers(c) == {}


def test_write_puts_it_under_the_cache_dir(tmp_path):
    c = cfg(tmp_path, SEARXNG_URL="http://127.0.0.1:8888")
    out = mcp_config.write(c)
    assert out == c.path("CACHE_DIR") / "mcp.json"
    assert json.loads(out.read_text(encoding="utf-8")) == mcp_config.build(c)
