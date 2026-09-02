"""Central MCP tool-annotation policy for the unified production server.

MCP hosts use ToolAnnotations as advisory behavior metadata. This module keeps
that metadata explicit and fail-closed: every registered production tool must
be classified as either semantically read-only or intentionally state-mutating
before the server is exposed to a client.

Transparent implementation caches used by read operations (for example the
SportsGameOdds team metadata cache) do not change user/provider state and are
classified as read-only for approval semantics. Tools whose purpose includes
changing active session state or persisted local application state are not.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations


READ_ONLY_TOOL_NAMES = frozenset(
    {
        "list_my_leagues",
        "get_league_context",
        "get_league_info",
        "get_league_settings",
        "get_league_standings",
        "get_league_snapshot",
        "discover_my_espn_leagues",
        "get_team_roster",
        "get_team_info",
        "get_all_rosters",
        "get_matchup_info",
        "get_player_stats",
        "get_free_agents",
        "compare_players",
        "get_player_intelligence",
        "get_consensus_rankings",
        "get_adp",
        "enrich_espn_free_agents",
        "analyze_my_team",
        "optimize_lineup",
        "get_fantasy_brief",
        "rank_waiver_targets",
        "evaluate_trade",
        "find_trade_targets",
        "get_draft_results",
        "get_draft_board",
        "analyze_draft_pick",
        "get_live_draft_brief",
        "get_commissioner_context",
        "get_commissioner_brief",
        "commissioner_audit_lineups",
        "commissioner_audit_rosters",
        "commissioner_audit_transactions",
        "commissioner_investigate",
        "get_sportsbook_usage",
        "find_sportsbook_team",
        "get_sportsbook_slate",
        "get_sportsbook_player_props",
        "compare_sportsbook_market",
        "find_sportsbook_market_disagreements",
        "find_sportsbook_player_prop_disagreements",
        "get_player_prop_market_context",
        "get_nfl_sportsbook_slate",
        "get_nfl_player_props",
        "get_fantasy_market_signal",
        "get_supported_sportsbook_leagues",
        "get_supported_sportsbooks",
    }
)

STATE_MUTATING_TOOL_NAMES = frozenset(
    {
        "authenticate",
        "logout",
        "sync_my_espn_leagues",
        "refresh_fantasypros_cache",
        "prepare_draft_strategy",
    }
)

EXPECTED_TOOL_NAMES = READ_ONLY_TOOL_NAMES | STATE_MUTATING_TOOL_NAMES


def apply_unified_tool_annotations(mcp) -> dict[str, int]:
    """Apply the production read/write hints to every registered FastMCP tool.

    The MCP Python SDK v1 FastMCP surface exposes its registered Tool objects
    through the tool manager used internally by ``list_tools``. Updating those
    Tool annotations here means the public ``tools/list`` response carries the
    same metadata that MCP hosts can inspect.

    The policy is intentionally fail-closed. A new or removed production tool
    raises at startup until this classification table is reviewed and updated.
    """

    manager = getattr(mcp, "_tool_manager", None)
    if manager is None or not hasattr(manager, "list_tools"):
        raise RuntimeError("FastMCP tool manager is unavailable; cannot apply annotation policy.")

    tools = list(manager.list_tools())
    actual_names = {tool.name for tool in tools}

    missing = EXPECTED_TOOL_NAMES - actual_names
    unknown = actual_names - EXPECTED_TOOL_NAMES
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing expected tools: {sorted(missing)}")
        if unknown:
            details.append(f"unclassified tools: {sorted(unknown)}")
        raise RuntimeError("MCP tool annotation policy mismatch: " + "; ".join(details))

    for tool in tools:
        if tool.name in READ_ONLY_TOOL_NAMES:
            tool.annotations = ToolAnnotations(readOnlyHint=True)
        else:
            tool.annotations = ToolAnnotations(readOnlyHint=False)

    return {
        "total": len(tools),
        "read_only": len(READ_ONLY_TOOL_NAMES),
        "state_mutating": len(STATE_MUTATING_TOOL_NAMES),
    }
