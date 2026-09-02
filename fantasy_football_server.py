"""Unified production entry point for Fantasy Football MCP."""

from __future__ import annotations

from importlib.metadata import version as distribution_version
import os

if not os.environ.get("ESPN_SWID") and os.environ.get("SWID"):
    os.environ["ESPN_SWID"] = os.environ["SWID"]

from espn_fantasy_server import SESSION_ID, api, authenticate, mcp
from espn_league_discovery import register_espn_league_discovery_tools
from mcp_tool_annotations import apply_unified_tool_annotations
from player_market_context_tools import register_player_market_context_tools
from sportsgameodds_disagreement_tools import register_sportsgameodds_disagreement_tools
from sportsgameodds_tools import register_sportsgameodds_tools

SERVER_VERSION = distribution_version("fantasy-football-mcp")

# FastMCP v1 does not expose a public version= constructor argument. Its
# low-level MCP Server does support one; without it, the initialize handshake
# reports the MCP SDK package version instead of this project's version.
mcp._mcp_server.version = SERVER_VERSION

api.prime(SESSION_ID)
register_sportsgameodds_tools(mcp)
register_sportsgameodds_disagreement_tools(mcp)
register_player_market_context_tools(mcp)
register_espn_league_discovery_tools(mcp)
apply_unified_tool_annotations(mcp)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
