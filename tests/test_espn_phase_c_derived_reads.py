import ast
import asyncio
import inspect
import textwrap
import unittest
from unittest.mock import AsyncMock, patch

import espn_fantasy_server as espn
from espn_transport import ESPNAccessError


class ESPNPhaseCDerivedReadsTests(unittest.TestCase):
    def test_enrich_free_agents_reuses_direct_free_agent_tool(self):
        direct_result = {
            "league_id": 55,
            "year": 2026,
            "week_used": 4,
            "position_filter": "RB",
            "count": 1,
            "free_agents": [{
                "name": "Direct Runner",
                "position": "RB",
                "proTeam": "ATL",
                "projected_points": 11.25,
                "points": 0,
                "pro_opponent": "BUF",
                "on_bye_week": False,
                "injured": False,
            }],
        }
        intel = {
            "ecr": 21,
            "pos_rank": "RB18",
            "tier": 4,
            "adp": 44.5,
            "projected_points": 182.0,
            "injury_status": None,
            "espn_ownership_pct": 68.0,
            "match_method": "name_team_position",
            "match_confidence": "high",
        }

        with patch.object(espn, "get_free_agents", new=AsyncMock(return_value=direct_result)) as direct, \
             patch.object(espn.fp_client, "build_player_intelligence", return_value=intel) as build_intel:
            result = asyncio.run(espn.enrich_espn_free_agents(55, position="RB", limit=7, year=2026))

        direct.assert_awaited_once_with(55, week=None, position="RB", size=7, year=2026)
        build_intel.assert_called_once_with("Direct Runner", "ATL", "RB")
        self.assertEqual(result, {
            "league_id": 55,
            "year": 2026,
            "week_used": 4,
            "position_filter": "RB",
            "count": 1,
            "free_agents": [{
                "player": "Direct Runner",
                "position": "RB",
                "nfl_team": "ATL",
                "espn_projected_points": 11.25,
                "fp_ecr": 21,
                "fp_pos_rank": "RB18",
                "fp_tier": 4,
                "fp_adp": 44.5,
                "fp_projected_points": 182.0,
                "injury_status": None,
                "espn_ownership_pct": 68.0,
                "match_method": "name_team_position",
                "match_confidence": "high",
            }],
        })

    def test_enrich_free_agents_forwards_direct_espn_errors(self):
        direct_error = {"error": "private_league_auth_required", "message": "Private league authentication is required."}
        with patch.object(espn, "get_free_agents", new=AsyncMock(return_value=direct_error)):
            result = asyncio.run(espn.enrich_espn_free_agents(55, year=2026))
        self.assertIs(result, direct_error)

    def test_enrich_source_has_no_wrapper_league_call(self):
        source = textwrap.dedent(inspect.getsource(espn.enrich_espn_free_agents))
        tree = ast.parse(source)
        wrapper_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_league"
        ]
        self.assertEqual(wrapper_calls, [])

    @staticmethod
    def _settings_payload(rec_points):
        return {
            "settings": {
                "name": "Scoring League",
                "size": 12,
                "scheduleSettings": {},
                "rosterSettings": {},
                "scoringSettings": {
                    "scoringType": "H2H_POINTS",
                    "scoringItems": [{"statId": 53, "points": rec_points}],
                },
            }
        }

    def test_registered_scoring_discovery_uses_project_owned_league_read(self):
        enabled = [
            ("ppr", {"league_id": 1}),
            ("half", {"league_id": 2}),
            ("blocked", {"league_id": 3}),
        ]
        payloads = {
            1: self._settings_payload(1.0),
            2: self._settings_payload(0.5),
        }

        def fetch(league_id, year):
            if league_id == 3:
                raise ESPNAccessError(403)
            return payloads[league_id]

        with patch.object(espn.league_registry, "load_registry", return_value={"version": 1}), \
             patch.object(espn.league_registry, "list_enabled_leagues", return_value=enabled), \
             patch.object(espn, "_fetch_core_league_payload", side_effect=fetch) as direct:
            result = espn._discover_registered_scoring_buckets()

        self.assertEqual(direct.call_count, 3)
        self.assertEqual(result["buckets"], ["PPR", "HALF"])
        self.assertEqual(result["leagues"], [
            {"alias": "ppr", "league_id": 1, "scoring_bucket": "PPR"},
            {"alias": "half", "league_id": 2, "scoring_bucket": "HALF"},
        ])
        self.assertEqual(result["failures"], [
            {"alias": "blocked", "league_id": 3, "status": "authentication_required"}
        ])

    def test_scoring_discovery_source_has_no_wrapper_league_call(self):
        source = textwrap.dedent(inspect.getsource(espn._discover_registered_scoring_buckets))
        tree = ast.parse(source)
        wrapper_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_league"
        ]
        self.assertEqual(wrapper_calls, [])


if __name__ == "__main__":
    unittest.main()
