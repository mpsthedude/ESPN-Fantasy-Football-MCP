import ast
import asyncio
import inspect
import textwrap
import unittest
from unittest.mock import AsyncMock, patch

import espn_fantasy_server as espn


class ESPNPhaseCWaiverRankingTests(unittest.TestCase):
    @staticmethod
    def _snapshot_payload():
        return {
            "seasonId": 2026,
            "scoringPeriodId": 4,
            "status": {"latestScoringPeriod": 4, "finalScoringPeriod": 17},
            "settings": {
                "name": "Waiver Test",
                "size": 1,
                "scheduleSettings": {},
                "rosterSettings": {"lineupSlotCounts": {"2": 1, "20": 1}},
                "scoringSettings": {
                    "scoringType": "H2H_POINTS",
                    "scoringItems": [{"statId": 53, "points": 1.0}],
                },
            },
            "draftDetail": {"drafted": True, "picks": [{"roundId": 1}]},
            "teams": [{
                "id": 7,
                "name": "Direct Waiver Team",
                "rankCalculatedFinal": 0,
                "playoffSeed": 1,
                "record": {"overall": {"wins": 2, "losses": 1, "pointsFor": 300, "pointsAgainst": 250}},
                "roster": {"entries": [{
                    "lineupSlotId": 2,
                    "playerPoolEntry": {"player": {
                        "fullName": "Roster Runner",
                        "eligibleSlots": [2, 23],
                        "proTeamId": 1,
                        "stats": [
                            {
                                "seasonId": 2026,
                                "statSplitTypeId": 0,
                                "scoringPeriodId": 0,
                                "statSourceId": 0,
                                "appliedTotal": 42.5,
                                "appliedAverage": 14.1,
                                "stats": {},
                                "appliedStats": {},
                            },
                            {
                                "seasonId": 2026,
                                "statSplitTypeId": 0,
                                "scoringPeriodId": 0,
                                "statSourceId": 1,
                                "appliedTotal": 175.25,
                                "appliedAverage": 10.3,
                                "stats": {},
                                "appliedStats": {},
                            },
                        ],
                    }},
                }]},
            }],
            "schedule": [],
        }

    def test_rank_waiver_targets_uses_direct_snapshot_for_team_roster_and_settings(self):
        payload = self._snapshot_payload()
        empty_fa = {
            "league_id": 55,
            "year": 2026,
            "week_used": 4,
            "position_filter": None,
            "count": 0,
            "free_agents": [],
        }
        fp_row = {
            "name": "Roster Runner",
            "position": "RB",
            "proTeam": "ATL",
            "projected_points": 175.25,
            "points": 42.5,
            "_fp_eval_value": 180.0,
            "_fp_intel": {"projected_points": 180.0, "match_confidence": "high"},
        }

        with patch.object(espn, "get_league_settings", new=AsyncMock(side_effect=AssertionError("separate settings read must not be used"))),  patch.object(espn, "_fetch_snapshot_payload", return_value=payload) as snapshot_fetch,  patch.object(espn, "_check_required_fp_caches", return_value=[]),  patch.object(espn.fp_client, "get_cache_freshness_report", return_value={}),  patch.object(espn.fp_client, "build_player_intelligence", return_value={"projected_points": 180.0, "match_confidence": "high"}) as build_intel,  patch.object(espn, "_build_fp_eval_roster", return_value=([fp_row], None)),  patch.object(espn, "get_free_agents", new=AsyncMock(return_value=empty_fa)) as free_agents:
            result = asyncio.run(espn.rank_waiver_targets(55, 7, year=2026))

        snapshot_fetch.assert_called_once_with(55, 2026)
        free_agents.assert_awaited_once_with(55, week=None, position=None, size=100, year=2026)
        build_intel.assert_any_call("Roster Runner", "ATL", "RB", scoring="PPR")
        self.assertEqual(result["league_id"], 55)
        self.assertEqual(result["team_id"], 7)
        self.assertEqual(result["team_name"], "Direct Waiver Team")
        self.assertEqual(result["scoring_bucket_detected"], "PPR")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["overall_recommendation"], "no_clear_upgrade_available")

    def test_rank_waiver_targets_preserves_invalid_team_error_contract(self):
        payload = self._snapshot_payload()
        with patch.object(espn, "_fetch_snapshot_payload", return_value=payload),  patch.object(espn, "_check_required_fp_caches", return_value=[]),  patch.object(espn.fp_client, "get_cache_freshness_report", return_value={}):
            result = asyncio.run(espn.rank_waiver_targets(55, 99, year=2026))

        self.assertEqual(result["error"], "invalid_parameter")
        self.assertIn("team_id=99", result["message"])
        self.assertIn("[7]", result["message"])

    def test_rank_waiver_targets_source_has_no_wrapper_league_call(self):
        func = inspect.unwrap(espn.rank_waiver_targets)
        source = textwrap.dedent(inspect.getsource(func))
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
