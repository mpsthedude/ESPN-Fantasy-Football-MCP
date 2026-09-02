import unittest

from espn_free_agent_read import (
    ESPNFreeAgentPayloadError,
    build_free_agent_filter,
    build_free_agents,
    build_pro_schedule,
    resolve_free_agent_week,
)


class ESPNFreeAgentReadTests(unittest.TestCase):
    def test_filter_matches_espn_api_046_contract(self):
        self.assertEqual(build_free_agent_filter(25, "RB"), {
            "players": {
                "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
                "filterSlotIds": {"value": [2]},
                "limit": 25,
                "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
                "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"},
            }
        })
        # Preserve legacy behavior: an unknown position does not fabricate a
        # slot id; ESPN receives an empty slot filter and returns all positions.
        self.assertEqual(
            build_free_agent_filter(10, "NOT_A_POSITION")["players"]["filterSlotIds"]["value"],
            [],
        )

    def test_default_week_uses_current_scoring_period_and_preseason_falls_back_to_one(self):
        current = {"scoringPeriodId": 7, "status": {"finalScoringPeriod": 18}}
        preseason = {"scoringPeriodId": 0, "status": {"finalScoringPeriod": 18}}
        postseason = {"scoringPeriodId": 20, "status": {"finalScoringPeriod": 18}}
        self.assertEqual(resolve_free_agent_week(current, None), 7)
        self.assertEqual(resolve_free_agent_week(preseason, None), 1)
        self.assertEqual(resolve_free_agent_week(postseason, None), 18)
        self.assertEqual(resolve_free_agent_week(current, 0), 7)
        self.assertEqual(resolve_free_agent_week(current, 3), 3)

    def test_pro_schedule_maps_opponents_and_omits_bye_teams(self):
        payload = {
            "settings": {
                "proTeams": [
                    {"id": 1, "proGamesByScoringPeriod": {"4": [{"awayProTeamId": 1, "homeProTeamId": 2}]}},
                    {"id": 2, "proGamesByScoringPeriod": {"4": [{"awayProTeamId": 1, "homeProTeamId": 2}]}},
                    {"id": 3, "proGamesByScoringPeriod": {"4": []}},
                ]
            }
        }
        self.assertEqual(build_pro_schedule(payload, 4), {1: 2, 2: 1})

    def test_player_rows_preserve_week_points_projection_opponent_bye_and_injury(self):
        players = {
            "players": [
                {
                    "player": {
                        "id": 101,
                        "fullName": "Running Back",
                        "eligibleSlots": [2, 20, 23],
                        "defaultPositionId": 2,
                        "proTeamId": 1,
                        "injured": False,
                        "stats": [
                            {"seasonId": 2026, "scoringPeriodId": 4, "statSourceId": 0, "statSplitTypeId": 1,
                             "proTeamId": 1, "appliedTotal": 18.126},
                            {"seasonId": 2026, "scoringPeriodId": 4, "statSourceId": 1, "statSplitTypeId": 1,
                             "appliedTotal": 15.444},
                        ],
                    }
                },
                {
                    "player": {
                        "id": 202,
                        "fullName": "Bye Receiver",
                        "eligibleSlots": [4, 20, 23],
                        "defaultPositionId": 4,
                        "proTeamId": 3,
                        "injured": True,
                        "stats": [
                            {"seasonId": 2026, "scoringPeriodId": 4, "statSourceId": 1, "statSplitTypeId": 1,
                             "appliedTotal": 0},
                        ],
                    }
                },
            ]
        }
        schedule = {
            "settings": {
                "proTeams": [
                    {"id": 1, "proGamesByScoringPeriod": {"4": [{"awayProTeamId": 1, "homeProTeamId": 2}]}},
                    {"id": 2, "proGamesByScoringPeriod": {"4": [{"awayProTeamId": 1, "homeProTeamId": 2}]}},
                    {"id": 3, "proGamesByScoringPeriod": {"4": []}},
                ]
            }
        }
        result = build_free_agents(players, schedule, 2026, 4)
        self.assertEqual(result[0], {
            "name": "Running Back",
            "position": "RB",
            "proTeam": "ATL",
            "projected_points": 15.44,
            "points": 18.13,
            "pro_opponent": "BUF",
            "on_bye_week": False,
            "injured": False,
        })
        self.assertEqual(result[1]["position"], "WR")
        self.assertEqual(result[1]["proTeam"], "CHI")
        self.assertIsNone(result[1]["pro_opponent"])
        self.assertTrue(result[1]["on_bye_week"])
        self.assertTrue(result[1]["injured"])

    def test_actual_week_stat_team_overrides_current_team_after_trade(self):
        players = {
            "players": [{
                "player": {
                    "fullName": "Traded Player",
                    "eligibleSlots": [4, 20],
                    "defaultPositionId": 4,
                    "proTeamId": 2,
                    "stats": [
                        {"seasonId": 2026, "scoringPeriodId": 4, "statSourceId": 0, "statSplitTypeId": 1,
                         "proTeamId": 1, "appliedTotal": 7},
                    ],
                }
            }]
        }
        schedule = {
            "settings": {"proTeams": [
                {"id": 1, "proGamesByScoringPeriod": {"4": [{"awayProTeamId": 1, "homeProTeamId": 3}]}},
                {"id": 3, "proGamesByScoringPeriod": {"4": [{"awayProTeamId": 1, "homeProTeamId": 3}]}},
            ]}
        }
        row = build_free_agents(players, schedule, 2026, 4)[0]
        self.assertEqual(row["proTeam"], "ATL")
        self.assertEqual(row["pro_opponent"], "CHI")

    def test_empty_player_result_does_not_require_schedule_shape(self):
        self.assertEqual(build_free_agents({"players": []}, None, 2026, 4), [])

    def test_bad_payloads_fail_closed(self):
        with self.assertRaises(ESPNFreeAgentPayloadError):
            build_free_agents({}, {}, 2026, 1)
        with self.assertRaises(ESPNFreeAgentPayloadError):
            build_free_agents({"players": [{}]}, {"settings": {"proTeams": []}}, 2026, 1)
        with self.assertRaises(ESPNFreeAgentPayloadError):
            build_pro_schedule({}, 1)
        with self.assertRaises(ValueError):
            resolve_free_agent_week({}, "1")


if __name__ == "__main__":
    unittest.main()
