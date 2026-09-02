import ast
import asyncio
import inspect
import textwrap
import unittest
from unittest.mock import patch

import espn_fantasy_server as espn
from espn_adapter import build_espn_league_snapshot_from_payload, build_espn_teams_from_payload


class ESPNPhaseCTeamAnalysisTests(unittest.TestCase):
    @staticmethod
    def _payload():
        def player_entry(name, eligible_slots, pro_team_id, actual, projected):
            return {
                "lineupSlotId": eligible_slots[0],
                "playerPoolEntry": {
                    "player": {
                        "fullName": name,
                        "eligibleSlots": eligible_slots,
                        "proTeamId": pro_team_id,
                        "stats": [
                            {
                                "seasonId": 2026,
                                "statSplitTypeId": 0,
                                "scoringPeriodId": 0,
                                "statSourceId": 0,
                                "appliedTotal": actual,
                                "appliedAverage": actual / 3,
                                "stats": {},
                                "appliedStats": {},
                            },
                            {
                                "seasonId": 2026,
                                "statSplitTypeId": 0,
                                "scoringPeriodId": 0,
                                "statSourceId": 1,
                                "appliedTotal": projected,
                                "appliedAverage": projected / 17,
                                "stats": {},
                                "appliedStats": {},
                            },
                        ],
                    }
                },
            }

        return {
            "seasonId": 2026,
            "scoringPeriodId": 4,
            "status": {
                "latestScoringPeriod": 4,
                "finalScoringPeriod": 17,
                "currentMatchupPeriod": 4,
            },
            "settings": {
                "name": "Analysis Test League",
                "size": 2,
                "scheduleSettings": {},
                "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 1, "20": 2}},
                "scoringSettings": {
                    "scoringType": "H2H_POINTS",
                    "scoringItems": [{"statId": 53, "points": 1.0}],
                },
            },
            "teams": [
                {
                    "id": 7,
                    "name": "Seven Team",
                    "record": {"overall": {"wins": 2, "losses": 1, "pointsFor": 300, "pointsAgainst": 250}},
                    "roster": {"entries": [player_entry("Seven Receiver", [4, 23], 21, 40.0, 210.5)]},
                },
                {
                    "id": 1,
                    "name": "One Team",
                    "record": {"overall": {"wins": 3, "losses": 0, "pointsFor": 330, "pointsAgainst": 220}},
                    "roster": {"entries": [player_entry("One Runner", [2, 23], 1, 55.0, 225.25)]},
                },
            ],
            "schedule": [],
            "draftDetail": {"drafted": True, "picks": [{"roundId": 1}]},
        }

    def test_raw_payload_adapter_matches_analysis_domain_contract(self):
        payload = self._payload()
        teams = build_espn_teams_from_payload(payload, 2026)
        self.assertEqual([team.team_id for team in teams], [1, 7])
        self.assertEqual(teams[0].team_name, "One Team")
        self.assertEqual(teams[0].roster[0].name, "One Runner")
        self.assertEqual(teams[0].roster[0].position, "RB")
        self.assertEqual(teams[0].roster[0].pro_team, "ATL")
        self.assertEqual(teams[0].roster[0].season_total_points, 55.0)
        self.assertEqual(teams[0].roster[0].season_projected_points, 225.25)

        snapshot = build_espn_league_snapshot_from_payload(
            payload, 55, 2026, {"QB": 1}, "PPR"
        )
        self.assertEqual(snapshot.platform, "espn")
        self.assertEqual(snapshot.league_id, 55)
        self.assertEqual(snapshot.scoring_bucket, "PPR")
        self.assertEqual([team.team_id for team in snapshot.teams], [1, 7])

    def test_analyze_my_team_uses_direct_payload_before_cache_gate(self):
        payload = self._payload()
        with patch.object(espn, "get_league_settings", side_effect=AssertionError("separate settings tool must not be used")),  patch.object(espn, "_fetch_snapshot_payload", return_value=payload) as fetch_snapshot,  patch.object(espn, "_check_required_fp_caches", return_value=["synthetic missing cache"]):
            result = asyncio.run(espn.analyze_my_team(55, 1, year=2026))

        fetch_snapshot.assert_called_once_with(55, 2026)
        self.assertEqual(result["error"], "cache_incomplete")
        self.assertEqual(result["scoring_bucket_detected"], "PPR")

    def test_analyze_my_team_preserves_invalid_team_error_with_direct_payload(self):
        payload = self._payload()
        with patch.object(espn, "_fetch_snapshot_payload", return_value=payload),  patch.object(espn, "_check_required_fp_caches", return_value=[]),  patch.object(espn.fp_client, "get_cache_freshness_report", return_value={}):
            result = asyncio.run(espn.analyze_my_team(55, 99, year=2026))

        self.assertEqual(result["error"], "invalid_parameter")
        self.assertIn("team_id=99", result["message"])
        self.assertIn("[1, 7]", result["message"])

    def test_analyze_my_team_source_has_no_wrapper_or_settings_tool_call(self):
        func = inspect.unwrap(espn.analyze_my_team)
        source = textwrap.dedent(inspect.getsource(func))
        tree = ast.parse(source)

        wrapper_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_league"
        ]
        settings_tool_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_league_settings"
        ]
        self.assertEqual(wrapper_calls, [])
        self.assertEqual(settings_tool_calls, [])


if __name__ == "__main__":
    unittest.main()
