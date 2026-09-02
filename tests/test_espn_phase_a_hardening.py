import ast
import asyncio
import importlib.util
import unittest
from unittest.mock import patch

import espn_fantasy_server as espn
from espn_transport import ESPNAccessError


class ESPNPhaseAHardeningTests(unittest.TestCase):
    def test_runtime_no_longer_depends_on_espn_api(self):
        self.assertIsNone(importlib.util.find_spec("espn_api"))

    def test_legacy_wrapper_cache_surface_is_retired(self):
        api = espn.ESPNFantasyFootballAPI()
        self.assertFalse(hasattr(api, "get_league"))
        self.assertFalse(hasattr(api, "leagues"))
        self.assertFalse(hasattr(api, "_league_cached_at"))

    def test_direct_core_read_tools_classify_project_transport_auth_errors_offline(self):
        tools = [
            lambda: espn.get_league_info(1, 2026),
            lambda: espn.get_team_info(1, 1, 2026),
            lambda: espn.get_league_standings(1, 2026),
        ]
        for call in tools:
            with self.subTest(tool=call):
                with patch.object(espn, "_fetch_core_league_payload", side_effect=ESPNAccessError(403)):
                    result = asyncio.run(call())
                self.assertIn("private league", result.lower())

        with patch.object(espn, "_fetch_core_league_payload", side_effect=ESPNAccessError(403)):
            result = asyncio.run(espn.get_league_settings(1, 2026))
        self.assertEqual(result["error"], "private_league_auth_required")

    def test_direct_roster_read_tools_classify_project_transport_auth_errors_offline(self):
        string_tools = [
            lambda: espn.get_team_roster(1, 1, 2026),
            lambda: espn.get_player_stats(1, "Nobody", 2026),
        ]
        for call in string_tools:
            with self.subTest(tool=call):
                with patch.object(espn, "_fetch_roster_payload", side_effect=ESPNAccessError(403)):
                    result = asyncio.run(call())
                self.assertIn("private league", result.lower())

        with patch.object(espn, "_fetch_roster_payload", side_effect=ESPNAccessError(403)):
            result = asyncio.run(espn.get_all_rosters(1, 2026))
        self.assertEqual(result["error"], "private_league_auth_required")

    def test_direct_matchup_read_classifies_project_transport_auth_errors_offline(self):
        with patch.object(espn, "_fetch_matchup_context_payload", side_effect=ESPNAccessError(403)):
            result = asyncio.run(espn.get_matchup_info(1, 1, 2026))
        self.assertIn("private league", result.lower())

    def test_direct_free_agent_read_classifies_project_transport_auth_errors_offline(self):
        with patch.object(espn, "_fetch_free_agent_context_payload", side_effect=ESPNAccessError(403)):
            result = asyncio.run(espn.get_free_agents(1, year=2026))
        self.assertEqual(result["error"], "private_league_auth_required")

    def test_standings_preserve_espn_standing_order_not_wins_points_resort(self):
        raw = {
            "members": [],
            "teams": [
                {"id": 2, "name": "More Wins But Seeded Second", "owners": [], "playoffSeed": 2,
                 "record": {"overall": {"wins": 9, "losses": 3, "pointsFor": 1400.0, "pointsAgainst": 1000.0}}},
                {"id": 1, "name": "Division Leader", "owners": [], "playoffSeed": 1,
                 "record": {"overall": {"wins": 7, "losses": 5, "pointsFor": 1200.0, "pointsAgainst": 1100.0}}},
            ],
        }

        with patch.object(espn, "_fetch_core_league_payload", return_value=raw):
            payload = ast.literal_eval(asyncio.run(espn.get_league_standings(1, 2026)))

        self.assertEqual([row["team_name"] for row in payload], [
            "Division Leader", "More Wins But Seeded Second"
        ])
        self.assertEqual([row["rank"] for row in payload], [1, 2])

    def test_matchup_validation_uses_league_configured_scoring_weeks(self):
        context = {
            "scoringPeriodId": 18,
            "status": {"currentMatchupPeriod": 18, "firstScoringPeriod": 1, "finalScoringPeriod": 18},
            "settings": {"scheduleSettings": {"matchupPeriods": {str(i): [i] for i in range(1, 19)}}},
        }
        score_payload = {"teams": [], "schedule": []}

        with patch.object(espn, "_fetch_matchup_context_payload", return_value=context), \
             patch.object(espn, "_fetch_matchup_score_payload", return_value=score_payload) as score_fetch:
            valid = asyncio.run(espn.get_matchup_info(1, week=18, year=2026))
            invalid = asyncio.run(espn.get_matchup_info(1, week=19, year=2026))

        self.assertEqual(valid, "[]")
        self.assertEqual(score_fetch.call_count, 1)
        self.assertIn("Valid scoring weeks", invalid)
        self.assertIn("18", invalid)

    def test_snapshot_uses_espn_standings_order(self):
        raw = {
            "seasonId": 2026,
            "scoringPeriodId": 1,
            "status": {"latestScoringPeriod": 1, "finalScoringPeriod": 17},
            "settings": {
                "name": "Test",
                "size": 2,
                "scheduleSettings": {"playoffTeamCount": 2},
                "rosterSettings": {"lineupSlotCounts": {}},
                "scoringSettings": {"scoringType": "H2H_POINTS", "scoringItems": []},
            },
            "draftDetail": {"drafted": False, "picks": []},
            "teams": [
                {"id": 2, "name": "Seed Two", "rankCalculatedFinal": 0, "playoffSeed": 2,
                 "record": {"overall": {"wins": 10, "losses": 2, "pointsFor": 1500.0, "pointsAgainst": 1000.0}},
                 "roster": {"entries": []}},
                {"id": 1, "name": "Seed One", "rankCalculatedFinal": 0, "playoffSeed": 1,
                 "record": {"overall": {"wins": 6, "losses": 6, "pointsFor": 1100.0, "pointsAgainst": 1090.0}},
                 "roster": {"entries": []}},
            ],
            "schedule": [],
        }

        with patch.object(espn, "_fetch_snapshot_payload", return_value=raw):
            result = asyncio.run(espn.get_league_snapshot(1, 2026, free_agent_limit=0))

        self.assertEqual([row["team_name"] for row in result["standings"]], ["Seed One", "Seed Two"])
        self.assertEqual([row["rank"] for row in result["standings"]], [1, 2])


if __name__ == "__main__":
    unittest.main()
