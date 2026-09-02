import unittest

from espn_league_read import (
    build_league_info,
    build_league_settings,
    build_standings,
    build_team_info,
)


SAMPLE = {
    "scoringPeriodId": 3,
    "status": {"latestScoringPeriod": 3, "finalScoringPeriod": 18},
    "settings": {
        "name": "Test League",
        "size": 2,
        "scoringSettings": {
            "scoringType": "H2H_POINTS",
            "scoringItems": [{"statId": 53, "points": 1.0}],
        },
        "scheduleSettings": {"playoffTeamCount": 2},
        "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 2}},
    },
    "members": [
        {"id": "owner-a", "displayName": "Owner A"},
        {"id": "owner-b", "displayName": "Owner B"},
    ],
    "teams": [
        {
            "id": 7,
            "name": "Alpha",
            "owners": ["owner-a"],
            "playoffSeed": 2,
            "record": {"overall": {"wins": 1, "losses": 1, "ties": 0, "pointsFor": 210.4, "pointsAgainst": 199.126}},
            "transactionCounter": {"acquisitions": 2, "drops": 1, "trades": 0},
            "currentSimulationResults": {"playoffPct": 0.75},
        },
        {
            "id": 11,
            "location": "Beta",
            "nickname": "Squad",
            "owners": ["owner-b"],
            "playoffSeed": 1,
            "record": {"overall": {"wins": 2, "losses": 0, "ties": 0, "pointsFor": 250.0, "pointsAgainst": 180.0}},
            "transactionCounter": {},
        },
    ],
    "schedule": [
        {"home": {"teamId": 7}, "away": {"teamId": 11}, "winner": "AWAY"},
        {"home": {"teamId": 11}, "away": {"teamId": 7}, "winner": "HOME"},
        {"home": {"teamId": 7}, "away": {"teamId": 11}, "winner": "UNDECIDED"},
    ],
}


class ESPNLeagueReadTests(unittest.TestCase):
    def test_builds_league_info_without_wrapper_objects(self):
        result = build_league_info(SAMPLE, 2026)
        self.assertEqual(result["name"], "Test League")
        self.assertEqual(result["current_week"], 3)
        self.assertEqual(result["nfl_week"], 3)
        self.assertEqual(result["team_count"], 2)
        self.assertEqual(result["teams"], ["Alpha", "Beta Squad"])

    def test_caps_current_week_at_final_scoring_period(self):
        payload = dict(SAMPLE)
        payload["scoringPeriodId"] = 99
        result = build_league_info(payload, 2026)
        self.assertEqual(result["current_week"], 18)

    def test_builds_settings_contract(self):
        result = build_league_settings(SAMPLE, 123, 2026)
        self.assertEqual(result["league_id"], 123)
        self.assertEqual(result["league_name"], "Test League")
        self.assertEqual(result["team_count"], 2)
        self.assertEqual(result["playoff_team_count"], 2)
        self.assertTrue(result["roster_slot_counts"])
        self.assertEqual(result["scoring_rules"][0]["id"], 53)
        self.assertEqual(result["scoring_rules"][0]["points"], 1.0)

    def test_standings_follow_espn_seed_order_and_preserve_owner_dicts(self):
        result = build_standings(SAMPLE)
        self.assertEqual([row["team_name"] for row in result], ["Beta Squad", "Alpha"])
        self.assertEqual(result[0]["rank"], 1)
        self.assertEqual(result[1]["points_against"], 199.13)
        self.assertEqual(result[0]["owner"][0]["displayName"], "Owner B")

    def test_team_info_matches_existing_summary_shape(self):
        result, valid_ids = build_team_info(SAMPLE, 7)
        self.assertEqual(valid_ids, [7, 11])
        self.assertEqual(result["team_name"], "Alpha")
        self.assertEqual(result["wins"], 1)
        self.assertEqual(result["playoff_pct"], 75.0)
        self.assertEqual(result["outcomes"], ["L", "L", "U"])

    def test_missing_team_returns_valid_ids(self):
        result, valid_ids = build_team_info(SAMPLE, 999)
        self.assertIsNone(result)
        self.assertEqual(valid_ids, [7, 11])


if __name__ == "__main__":
    unittest.main()
