import ast
import asyncio
import inspect
import textwrap
import unittest
from unittest.mock import patch

import espn_fantasy_server as espn
from espn_roster_read import build_lineup_team


class ESPNPhaseCLineupOptimizerTests(unittest.TestCase):
    @staticmethod
    def _league_payload():
        return {
            "seasonId": 2026,
            "scoringPeriodId": 5,
            "status": {
                "latestScoringPeriod": 5,
                "finalScoringPeriod": 18,
                "currentMatchupPeriod": 5,
            },
            "settings": {
                "name": "Lineup Test League",
                "size": 1,
                "scheduleSettings": {},
                "rosterSettings": {"lineupSlotCounts": {"2": 1, "20": 1}},
                "scoringSettings": {
                    "scoringType": "H2H_POINTS",
                    "scoringItems": [{"statId": 53, "points": 1.0}],
                },
            },
            "teams": [{
                "id": 7,
                "name": "Direct Lineup Team",
                "roster": {"entries": [{
                    "lineupSlotId": 20,
                    "playerPoolEntry": {"player": {
                        "fullName": "Roster Runner",
                        "eligibleSlots": [2, 23],
                        "proTeamId": 1,
                        "injuryStatus": "QUESTIONABLE",
                        "stats": [
                            {
                                "seasonId": 2026,
                                "statSplitTypeId": 0,
                                "scoringPeriodId": 0,
                                "statSourceId": 0,
                                "appliedTotal": 44.5,
                                "appliedAverage": 14.8,
                                "stats": {},
                                "appliedStats": {},
                            },
                            {
                                "seasonId": 2026,
                                "statSplitTypeId": 0,
                                "scoringPeriodId": 0,
                                "statSourceId": 1,
                                "appliedTotal": 221.25,
                                "appliedAverage": 13.0,
                                "stats": {},
                                "appliedStats": {},
                            },
                            {
                                "seasonId": 2026,
                                "statSplitTypeId": 0,
                                "scoringPeriodId": 5,
                                "statSourceId": 1,
                                "appliedTotal": 16.75,
                                "appliedAverage": 16.75,
                                "stats": {},
                                "appliedStats": {},
                            },
                        ],
                    }},
                }]},
            }],
            "schedule": [],
            "draftDetail": {"drafted": True, "picks": [{"roundId": 1}]},
        }

    @staticmethod
    def _schedule_payload():
        games = {}
        for week in range(1, 19):
            if week == 7:
                continue
            games[str(week)] = [{
                "awayProTeamId": 1,
                "homeProTeamId": 2,
                "date": 1780000000000 + week * 1000,
            }]
        return {
            "settings": {
                "proTeams": [
                    {"id": 0, "proGamesByScoringPeriod": {}},
                    {"id": 1, "proGamesByScoringPeriod": games},
                    {"id": 2, "proGamesByScoringPeriod": {}},
                ]
            }
        }

    def test_direct_lineup_parser_preserves_wrapper_weekly_contract(self):
        team, valid_ids = build_lineup_team(
            self._league_payload(), self._schedule_payload(), 7, 2026
        )
        self.assertEqual(valid_ids, [7])
        self.assertEqual(team["team_name"], "Direct Lineup Team")
        row = team["roster"][0]

        self.assertEqual(row["name"], "Roster Runner")
        self.assertEqual(row["position"], "RB")
        self.assertEqual(row["proTeam"], "ATL")
        self.assertIsNone(row["projected_points"])
        self.assertEqual(row["points"], 44.5)
        self.assertEqual(row["projected_total_points"], 221.25)
        self.assertEqual(row["lineup_slot"], "BE")
        self.assertEqual(row["injury_status"], "QUESTIONABLE")
        self.assertEqual(row["eligible_slots"], ["RB", "RB/WR/TE"])
        self.assertEqual(row["_ol_raw_stats"][5]["projected_points"], 16.75)
        self.assertEqual(len(row["_ol_raw_schedule"]), 17)
        self.assertNotIn("7", row["_ol_raw_schedule"])
        self.assertEqual(row["_ol_raw_schedule"]["5"]["team"], "BUF")

        sufficient, normalized = espn._ol_schedule_sufficient(row["_ol_raw_schedule"])
        self.assertTrue(sufficient)
        self.assertEqual(len(normalized), 17)
        self.assertNotIn(7, normalized)

    def test_optimize_lineup_uses_project_owned_reads_before_cache_gate(self):
        with patch.object(
            espn, "_fetch_snapshot_payload", return_value=self._league_payload()
        ) as snapshot_fetch, patch.object(
            espn, "_fetch_pro_schedule_payload", return_value=self._schedule_payload()
        ) as schedule_fetch, patch.object(
            espn, "_check_required_fp_caches", return_value=["synthetic missing cache"]
        ):
            result = asyncio.run(espn.optimize_lineup(55, 7, year=2026))

        snapshot_fetch.assert_called_once_with(55, 2026)
        schedule_fetch.assert_called_once_with(2026)
        self.assertEqual(result["error"], "cache_incomplete")
        self.assertEqual(result["scoring_bucket_detected"], "PPR")

    def test_optimize_lineup_preserves_invalid_team_contract(self):
        with patch.object(
            espn, "_fetch_snapshot_payload", return_value=self._league_payload()
        ), patch.object(
            espn, "_fetch_pro_schedule_payload", return_value=self._schedule_payload()
        ):
            result = asyncio.run(espn.optimize_lineup(55, 99, year=2026))

        self.assertEqual(result["error"], "invalid_parameter")
        self.assertIn("team_id=99", result["message"])
        self.assertIn("[7]", result["message"])

    def test_optimize_lineup_source_has_no_wrapper_league_call(self):
        source = textwrap.dedent(inspect.getsource(inspect.unwrap(espn.optimize_lineup)))
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
