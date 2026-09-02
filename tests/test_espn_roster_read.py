import unittest

from espn_reference import PRO_TEAM_MAP
from espn_roster_read import (
    build_all_rosters,
    build_player_stats,
    build_team_roster,
    parse_roster_entry,
)


PRO_TEAM_ID = next(team_id for team_id, name in PRO_TEAM_MAP.items() if name == "LAR")


def player_entry(name="Matthew Stafford", *, lineup_slot_id=0, injured=False):
    return {
        "playerId": 123,
        "lineupSlotId": lineup_slot_id,
        "playerPoolEntry": {
            "id": 123,
            "player": {
                "id": 123,
                "fullName": name,
                "eligibleSlots": [0, 20, 21],
                "defaultPositionId": 0,
                "proTeamId": PRO_TEAM_ID,
                "injured": injured,
                "stats": [
                    {
                        "seasonId": 2026,
                        "scoringPeriodId": 0,
                        "statSourceId": 0,
                        "statSplitTypeId": 0,
                        "appliedTotal": 123.456,
                        "appliedAverage": 10.288,
                        "stats": {"3": 300},
                        "appliedStats": {"3": 12.0},
                    },
                    {
                        "seasonId": 2026,
                        "scoringPeriodId": 0,
                        "statSourceId": 1,
                        "statSplitTypeId": 0,
                        "appliedTotal": 250.123,
                        "appliedAverage": 14.0,
                        "stats": {"3": 4200},
                        "appliedStats": {"3": 250.123},
                    },
                    {
                        "seasonId": 2026,
                        "scoringPeriodId": 1,
                        "statSourceId": 0,
                        "statSplitTypeId": 1,
                        "appliedTotal": 20.5,
                        "appliedAverage": 20.5,
                        "stats": {"3": 250},
                        "appliedStats": {"3": 20.5},
                    },
                    {
                        "seasonId": 2025,
                        "scoringPeriodId": 0,
                        "statSourceId": 0,
                        "statSplitTypeId": 0,
                        "appliedTotal": 999.0,
                        "stats": {},
                        "appliedStats": {},
                    },
                ],
            },
        },
    }


SAMPLE = {
    "members": [
        {"id": "owner-a", "displayName": "Owner A"},
        {"id": "owner-b", "displayName": "Owner B"},
    ],
    "teams": [
        {
            "id": 3,
            "name": "Team Three",
            "owners": ["owner-a"],
            "record": {"overall": {"wins": 5, "losses": 2}},
            "roster": {"entries": [player_entry()]},
        },
        {
            "id": 7,
            "location": "Team",
            "nickname": "Seven",
            "owners": ["owner-b"],
            "record": {"overall": {"wins": 2, "losses": 5}},
            "roster": {"entries": [player_entry("Backup Quarterback", lineup_slot_id=20, injured=True)]},
        },
    ],
}


class ESPNRosterReadTests(unittest.TestCase):
    def test_parse_entry_preserves_player_contract(self):
        result = parse_roster_entry(player_entry(), 2026)
        self.assertEqual(result["name"], "Matthew Stafford")
        self.assertEqual(result["position"], "QB")
        self.assertEqual(result["proTeam"], "LAR")
        self.assertEqual(result["points"], 123.46)
        self.assertEqual(result["projected_points"], 250.12)
        self.assertEqual(result["lineup_slot"], "QB")
        self.assertFalse(result["injured"])
        self.assertIn(0, result["stats"])
        self.assertIn(1, result["stats"])
        self.assertEqual(result["stats"][1]["points"], 20.5)

    def test_team_roster_preserves_owner_record_and_player_stats(self):
        result, valid_ids = build_team_roster(SAMPLE, 3, 2026)
        self.assertEqual(valid_ids, [3, 7])
        self.assertEqual(result["team_name"], "Team Three")
        self.assertEqual(result["owner"][0]["displayName"], "Owner A")
        self.assertEqual(result["wins"], 5)
        self.assertEqual(result["losses"], 2)
        self.assertEqual(result["roster"][0]["name"], "Matthew Stafford")
        self.assertIn("stats", result["roster"][0])

    def test_missing_team_returns_valid_ids(self):
        result, valid_ids = build_team_roster(SAMPLE, 999, 2026)
        self.assertIsNone(result)
        self.assertEqual(valid_ids, [3, 7])

    def test_player_stats_searches_roster_order_and_is_case_insensitive(self):
        result = build_player_stats(SAMPLE, "staff", 2026)
        self.assertEqual(result["name"], "Matthew Stafford")
        self.assertEqual(result["team"], "LAR")
        self.assertEqual(result["points"], 123.46)
        self.assertEqual(result["projected_points"], 250.12)
        self.assertFalse(result["injured"])

    def test_player_stats_returns_none_when_not_rostered(self):
        self.assertIsNone(build_player_stats(SAMPLE, "Nobody", 2026))

    def test_all_rosters_lightweight_contract(self):
        result = build_all_rosters(SAMPLE, 123456789, 2026, detailed=False)
        self.assertEqual(set(result), {"league_id", "year", "team_count", "teams"})
        self.assertEqual(result["team_count"], 2)
        self.assertEqual([team["team_id"] for team in result["teams"]], [3, 7])
        player = result["teams"][0]["roster"][0]
        self.assertEqual(set(player), {
            "name", "position", "proTeam", "projected_points", "points", "lineup_slot"
        })
        self.assertEqual(player["lineup_slot"], "QB")

    def test_all_rosters_detailed_adds_stats_only(self):
        result = build_all_rosters(SAMPLE, 123456789, 2026, detailed=True)
        player = result["teams"][0]["roster"][0]
        self.assertIn("stats", player)
        self.assertIn(0, player["stats"])


if __name__ == "__main__":
    unittest.main()
