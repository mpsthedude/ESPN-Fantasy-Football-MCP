"""D5C: MCP public tool-contract regression guard.

Locks the v0.1 PUBLIC MCP TOOL CONTRACT for espn_fantasy_server.mcp:

    - exact tool count (37)
    - exact tool names
    - semantically normalized input-parameter schemas (names, required vs
      optional, primitive/array/object types, meaningful defaults)

Design notes (see D5C discovery for full empirical detail):

    - `mcp.server.fastmcp.FastMCP.list_tools()` is ASYNC and returns
      `list[mcp.types.Tool]`. Each Tool exposes `.name`, `.description`,
      `.inputSchema` (a plain JSON-schema-shaped dict) on every currently
      supported mcp release (>=1.5.0,<2).

    - Empirically diffed this project's actual `inputSchema` output for all
      37 tools under BOTH mcp==1.5.0 (this repo's locked version) and a
      freshly-resolved mcp==1.29.0 (newest 1.x at time of writing): the
      `inputSchema` payload is byte-identical across both releases for
      every tool, except for a per-property `"title"` key (e.g.
      `{"title": "Espn S2", "type": "string"}`). That `title` is Pydantic's
      auto-generated, non-authored, capitalize-the-field-name string - this
      project never sets an explicit `title=` anywhere in its tool
      signatures (verified against source). It is stripped before
      comparison as non-semantic framework noise.

    - mcp==1.29.0 additionally exposes newer optional `Tool` fields absent
      entirely from mcp==1.5.0's model: `title` (tool-level, always None
      here), `outputSchema`, `icons`, `annotations`, `meta`, `execution`.
      Of these, `outputSchema` is the only one that is ever non-None for
      this project's tools, and it is auto-derived by the framework purely
      from a `-> str` Python return-type annotation on 8 of the 37 tools -
      it exists under mcp 1.29.0 and does not exist at all under mcp 1.5.0
      for the exact same source code. This is a framework-version artifact,
      not a project-authored, stable public contract, so per design it is
      DELIBERATELY EXCLUDED from this guard entirely (not merely
      normalized-away). Output behavior remains covered by the project's
      existing functional/behavioral test suite. Only `inputSchema` is a
      contract this project actually controls and should lock.

    - `ToolManager._tools` is a plain `dict` keyed by tool name, so the
      registry structurally cannot contain duplicate names - a second
      `@mcp.tool()` registration under an existing name simply overwrites
      the first entry rather than creating a second one. No separate
      "no duplicate names" test is added for this reason (would be
      meaningless coverage of a Python dict's own key-uniqueness
      guarantee); the exact-count and exact-name-set tests already fully
      constrain the registry's effective content.

Stdlib only. No network. No credentials. No production code changes.
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import espn_fantasy_server as srv


def normalize_schema(schema):
    """Recursively normalize a JSON-schema-shaped dict/list for semantic
    (not representational) comparison.

    Removes:
        - "title" keys at any depth (Pydantic auto-generates these from
          parameter names; this project never authors them explicitly).

    Normalizes:
        - "required" lists are sorted, since a JSON-schema "required" list
          denotes an unordered set of mandatory property names, not a
          meaningful sequence.

    Preserves everything else verbatim: "properties", nested "type",
    "default", "enum", "items", "anyOf"/"oneOf", and any "$defs", so a real
    change to parameter names, required-ness, types, defaults, or
    constraints will still fail comparison.
    """
    if isinstance(schema, dict):
        out = {}
        for key, value in schema.items():
            if key == "title":
                continue
            if key == "required" and isinstance(value, list):
                out[key] = sorted(value)
            else:
                out[key] = normalize_schema(value)
        return out
    if isinstance(schema, list):
        return [normalize_schema(item) for item in schema]
    return schema


# Fixed public contract for the v0.1 tool surface, established from the
# actual committed runtime registration at HEAD da8cc3e, then manually
# reviewed tool-by-tool before being embedded here (see D5C report).
EXPECTED_TOOL_NAMES = {
    "analyze_draft_pick",
    "analyze_my_team",
    "authenticate",
    "commissioner_audit_lineups",
    "commissioner_audit_rosters",
    "commissioner_audit_transactions",
    "commissioner_investigate",
    "compare_players",
    "enrich_espn_free_agents",
    "evaluate_trade",
    "find_trade_targets",
    "get_adp",
    "get_all_rosters",
    "get_commissioner_brief",
    "get_commissioner_context",
    "get_consensus_rankings",
    "get_draft_board",
    "get_draft_results",
    "get_fantasy_brief",
    "get_free_agents",
    "get_league_context",
    "get_league_info",
    "get_league_settings",
    "get_league_snapshot",
    "get_league_standings",
    "get_live_draft_brief",
    "get_matchup_info",
    "get_player_intelligence",
    "get_player_stats",
    "get_team_info",
    "get_team_roster",
    "list_my_leagues",
    "logout",
    "optimize_lineup",
    "prepare_draft_strategy",
    "rank_waiver_targets",
    "refresh_fantasypros_cache",
}

EXPECTED_TOOL_COUNT = 37
assert len(EXPECTED_TOOL_NAMES) == EXPECTED_TOOL_COUNT, (
    "internal fixture error: EXPECTED_TOOL_NAMES literal does not contain "
    f"{EXPECTED_TOOL_COUNT} unique entries"
)

# Normalized (see normalize_schema docstring) input-parameter contract per
# tool, captured from the actual committed source at HEAD da8cc3e and
# confirmed byte-identical (modulo the stripped "title" noise) under both
# mcp==1.5.0 and a freshly-resolved mcp==1.29.0.
EXPECTED_TOOL_SCHEMAS = {
    "analyze_draft_pick": {
        "properties": {
            "alias": {"default": None, "type": "string"},
            "candidate_player_ids": {"default": None, "items": {}, "type": "array"},
            "candidate_player_names": {"default": None, "items": {}, "type": "array"},
            "league_id": {"default": None, "type": "integer"},
            "top_n": {"default": 5, "type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "type": "object",
    },
    "analyze_my_team": {
        "properties": {
            "league_id": {"type": "integer"},
            "team_id": {"type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "required": ["league_id", "team_id"],
        "type": "object",
    },
    "authenticate": {
        "properties": {
            "espn_s2": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
            "swid": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        },
        "type": "object",
    },
    "commissioner_audit_lineups": {
        "properties": {
            "alias": {"default": None, "type": "string"},
            "league_id": {"default": None, "type": "integer"},
            "team_id": {"default": None, "type": "integer"},
            "week": {"default": None, "type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "type": "object",
    },
    "commissioner_audit_rosters": {
        "properties": {
            "alias": {"default": None, "type": "string"},
            "league_id": {"default": None, "type": "integer"},
            "team_id": {"default": None, "type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "type": "object",
    },
    "commissioner_audit_transactions": {
        "properties": {
            "action_types": {"default": None, "items": {}, "type": "array"},
            "alias": {"default": None, "type": "string"},
            "end_timestamp_ms": {"default": None, "type": "integer"},
            "league_id": {"default": None, "type": "integer"},
            "limit": {"default": 50, "type": "integer"},
            "player_id": {"default": None, "type": "integer"},
            "player_name": {"default": None, "type": "string"},
            "start_timestamp_ms": {"default": None, "type": "integer"},
            "team_id": {"default": None, "type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "type": "object",
    },
    "commissioner_investigate": {
        "properties": {
            "action_types": {"default": None, "items": {}, "type": "array"},
            "alias": {"default": None, "type": "string"},
            "end_date": {"default": None, "type": "string"},
            "include_lineup_evidence": {"default": True, "type": "boolean"},
            "include_roster_evidence": {"default": True, "type": "boolean"},
            "include_transaction_evidence": {"default": True, "type": "boolean"},
            "league_id": {"default": None, "type": "integer"},
            "other_team_id": {"default": None, "type": "integer"},
            "player_id": {"default": None, "type": "integer"},
            "player_name": {"default": None, "type": "string"},
            "start_date": {"default": None, "type": "string"},
            "team_id": {"default": None, "type": "integer"},
            "week": {"default": None, "type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "type": "object",
    },
    "compare_players": {
        "properties": {"players": {"items": {}, "type": "array"}},
        "required": ["players"],
        "type": "object",
    },
    "enrich_espn_free_agents": {
        "properties": {
            "league_id": {"type": "integer"},
            "limit": {"default": 25, "type": "integer"},
            "position": {"default": None, "type": "string"},
            "year": {"default": None, "type": "integer"},
        },
        "required": ["league_id"],
        "type": "object",
    },
    "evaluate_trade": {
        "properties": {
            "league_id": {"type": "integer"},
            "players_in": {"items": {}, "type": "array"},
            "players_out": {"items": {}, "type": "array"},
            "team_id": {"type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "required": ["league_id", "players_in", "players_out", "team_id"],
        "type": "object",
    },
    "find_trade_targets": {
        "properties": {
            "league_id": {"type": "integer"},
            "limit": {"default": 10, "type": "integer"},
            "max_package_size": {"default": 2, "type": "integer"},
            "partner_team_id": {"default": None, "type": "integer"},
            "position": {"default": None, "type": "string"},
            "team_id": {"type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "required": ["league_id", "team_id"],
        "type": "object",
    },
    "get_adp": {
        "properties": {
            "limit": {"default": None, "type": "integer"},
            "position": {"type": "string"},
        },
        "required": ["position"],
        "type": "object",
    },
    "get_all_rosters": {
        "properties": {
            "detailed": {"default": False, "type": "boolean"},
            "league_id": {"type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "required": ["league_id"],
        "type": "object",
    },
    "get_commissioner_brief": {
        "properties": {
            "alias": {"default": None, "type": "string"},
            "inactivity_lookback_weeks": {"default": 3, "type": "integer"},
            "league_id": {"default": None, "type": "integer"},
            "recent_activity_limit": {"default": 25, "type": "integer"},
            "week": {"default": None, "type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "type": "object",
    },
    "get_commissioner_context": {
        "properties": {
            "alias": {"default": None, "type": "string"},
            "league_id": {"default": None, "type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "type": "object",
    },
    "get_consensus_rankings": {
        "properties": {
            "limit": {"default": None, "type": "integer"},
            "position": {"type": "string"},
            "scoring": {"default": "PPR", "type": "string"},
        },
        "required": ["position"],
        "type": "object",
    },
    "get_draft_board": {
        "properties": {
            "alias": {"default": None, "type": "string"},
            "league_id": {"default": None, "type": "integer"},
            "top_available": {"default": 50, "type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "type": "object",
    },
    "get_draft_results": {
        "properties": {
            "league_id": {"type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "required": ["league_id"],
        "type": "object",
    },
    "get_fantasy_brief": {
        "properties": {
            "league_id": {"type": "integer"},
            "team_id": {"type": "integer"},
            "week": {"default": None, "type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "required": ["league_id", "team_id"],
        "type": "object",
    },
    "get_free_agents": {
        "properties": {
            "league_id": {"type": "integer"},
            "position": {"default": None, "type": "string"},
            "size": {"default": 50, "type": "integer"},
            "week": {"default": None, "type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "required": ["league_id"],
        "type": "object",
    },
    "get_league_context": {
        "properties": {
            "alias": {"default": None, "type": "string"},
            "league_id": {"default": None, "type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "type": "object",
    },
    "get_league_info": {
        "properties": {
            "league_id": {"type": "integer"},
            "year": {"default": 2026, "type": "integer"},
        },
        "required": ["league_id"],
        "type": "object",
    },
    "get_league_settings": {
        "properties": {
            "league_id": {"type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "required": ["league_id"],
        "type": "object",
    },
    "get_league_snapshot": {
        "properties": {
            "free_agent_limit": {"default": 25, "type": "integer"},
            "league_id": {"type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "required": ["league_id"],
        "type": "object",
    },
    "get_league_standings": {
        "properties": {
            "league_id": {"type": "integer"},
            "year": {"default": 2026, "type": "integer"},
        },
        "required": ["league_id"],
        "type": "object",
    },
    "get_live_draft_brief": {
        "properties": {
            "alias": {"default": None, "type": "string"},
            "league_id": {"default": None, "type": "integer"},
            "top_n": {"default": 5, "type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "type": "object",
    },
    "get_matchup_info": {
        "properties": {
            "league_id": {"type": "integer"},
            "week": {"default": None, "type": "integer"},
            "year": {"default": 2026, "type": "integer"},
        },
        "required": ["league_id"],
        "type": "object",
    },
    "get_player_intelligence": {
        "properties": {
            "player_name": {"type": "string"},
            "position": {"default": None, "type": "string"},
            "team": {"default": None, "type": "string"},
        },
        "required": ["player_name"],
        "type": "object",
    },
    "get_player_stats": {
        "properties": {
            "league_id": {"type": "integer"},
            "player_name": {"type": "string"},
            "year": {"default": 2026, "type": "integer"},
        },
        "required": ["league_id", "player_name"],
        "type": "object",
    },
    "get_team_info": {
        "properties": {
            "league_id": {"type": "integer"},
            "team_id": {"type": "integer"},
            "year": {"default": 2026, "type": "integer"},
        },
        "required": ["league_id", "team_id"],
        "type": "object",
    },
    "get_team_roster": {
        "properties": {
            "league_id": {"type": "integer"},
            "team_id": {"type": "integer"},
            "year": {"default": 2026, "type": "integer"},
        },
        "required": ["league_id", "team_id"],
        "type": "object",
    },
    "list_my_leagues": {
        "properties": {"year": {"default": None, "type": "integer"}},
        "type": "object",
    },
    "logout": {
        "properties": {},
        "type": "object",
    },
    "optimize_lineup": {
        "properties": {
            "league_id": {"type": "integer"},
            "team_id": {"type": "integer"},
            "week": {"default": None, "type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "required": ["league_id", "team_id"],
        "type": "object",
    },
    "prepare_draft_strategy": {
        "properties": {
            "alias": {"default": None, "type": "string"},
            "horizon_open_picks": {"default": 8, "type": "integer"},
            "league_id": {"default": None, "type": "integer"},
            "save_strategy": {"default": True, "type": "boolean"},
            "year": {"default": None, "type": "integer"},
        },
        "type": "object",
    },
    "rank_waiver_targets": {
        "properties": {
            "league_id": {"type": "integer"},
            "limit": {"default": 10, "type": "integer"},
            "position": {"default": None, "type": "string"},
            "team_id": {"type": "integer"},
            "year": {"default": None, "type": "integer"},
        },
        "required": ["league_id", "team_id"],
        "type": "object",
    },
    "refresh_fantasypros_cache": {
        "properties": {
            "allow_soft_limit_override": {"default": False, "type": "boolean"},
            "datasets": {"default": None, "items": {}, "type": "array"},
            "dry_run": {"default": False, "type": "boolean"},
            "force": {"default": False, "type": "boolean"},
            "positions": {"default": None, "items": {}, "type": "array"},
            "scoring": {"default": "REGISTERED", "type": "string"},
        },
        "type": "object",
    },
}

assert set(EXPECTED_TOOL_SCHEMAS.keys()) == EXPECTED_TOOL_NAMES, (
    "internal fixture error: EXPECTED_TOOL_SCHEMAS keys do not match "
    "EXPECTED_TOOL_NAMES"
)


class TestMcpToolContract(unittest.TestCase):
    """Locks the public MCP tool contract: count, names, input schemas."""

    @classmethod
    def setUpClass(cls):
        # FastMCP.list_tools() is async; run it once for the whole class.
        cls.actual_tools = asyncio.run(srv.mcp.list_tools())
        cls.actual_by_name = {t.name: t for t in cls.actual_tools}

    def test_exact_tool_count(self):
        self.assertEqual(
            len(self.actual_tools),
            EXPECTED_TOOL_COUNT,
            f"expected exactly {EXPECTED_TOOL_COUNT} registered MCP tools, "
            f"found {len(self.actual_tools)}",
        )

    def test_exact_tool_names(self):
        actual_names = set(self.actual_by_name.keys())
        missing = EXPECTED_TOOL_NAMES - actual_names
        unexpected = actual_names - EXPECTED_TOOL_NAMES
        self.assertEqual(
            actual_names,
            EXPECTED_TOOL_NAMES,
            "tool name contract violated.\n"
            f"  missing (expected but not registered): {sorted(missing)}\n"
            f"  unexpected (registered but not expected): {sorted(unexpected)}",
        )

    def test_exact_input_schemas(self):
        for tool_name, expected_schema in EXPECTED_TOOL_SCHEMAS.items():
            with self.subTest(tool=tool_name):
                tool = self.actual_by_name.get(tool_name)
                if tool is None:
                    self.fail(
                        f"tool '{tool_name}' is expected but not registered "
                        "(see test_exact_tool_names for the full diff)"
                    )
                    continue
                actual_normalized = normalize_schema(tool.inputSchema)
                self.assertEqual(
                    actual_normalized,
                    expected_schema,
                    f"input schema contract violated for tool '{tool_name}'.\n"
                    f"  expected: {json.dumps(expected_schema, sort_keys=True)}\n"
                    f"  actual:   {json.dumps(actual_normalized, sort_keys=True)}",
                )


if __name__ == "__main__":
    unittest.main()
