import unittest

from espn_draft_read import ESPNDraftPayloadError, build_draft_results


class ESPNDraftReadTests(unittest.TestCase):
    def test_completed_snake_draft_preserves_existing_response_shape(self):
        draft = {
            "draftDetail": {
                "drafted": True,
                "picks": [
                    {
                        "roundId": 1,
                        "roundPickNumber": 2,
                        "playerId": 101,
                        "teamId": 7,
                        "keeper": False,
                        "bidAmount": None,
                        "nominatingTeamId": 0,
                    }
                ],
            },
            "teams": [{"id": 7, "name": "Seven Nation Army"}],
        }
        players = [{"id": 101, "fullName": "Drafted Player"}]

        result = build_draft_results(draft, players, 999, 2026)

        self.assertEqual(result["league_id"], 999)
        self.assertEqual(result["year"], 2026)
        self.assertTrue(result["drafted"])
        self.assertEqual(result["pick_count"], 1)
        self.assertEqual(result["picks"], [{
            "round": 1,
            "pick_in_round": 2,
            "player_name": "Drafted Player",
            "team_id": 7,
            "team_name": "Seven Nation Army",
            "keeper": False,
            "bid_amount": None,
            "nominating_team_id": None,
            "nominating_team_name": None,
        }])

    def test_auction_fields_and_location_nickname_fallback(self):
        draft = {
            "draftDetail": {
                "drafted": True,
                "picks": [{
                    "roundId": 0,
                    "roundPickNumber": 0,
                    "playerId": 202,
                    "teamId": 3,
                    "keeper": True,
                    "bidAmount": 37,
                    "nominatingTeamId": 4,
                }],
            },
            "teams": [
                {"id": 3, "location": "Music", "nickname": "City"},
                {"id": 4, "name": "Nominator"},
            ],
        }
        players = [{"id": 202, "fullName": "Auction Player"}]

        pick = build_draft_results(draft, players, 1, 2026)["picks"][0]
        self.assertEqual(pick["team_name"], "Music City")
        self.assertEqual(pick["nominating_team_id"], 4)
        self.assertEqual(pick["nominating_team_name"], "Nominator")
        self.assertEqual(pick["bid_amount"], 37)
        self.assertTrue(pick["keeper"])

    def test_not_drafted_matches_current_tool_contract_without_needing_player_map(self):
        result = build_draft_results(
            {"draftDetail": {"drafted": False}, "teams": []},
            [],
            77,
            2026,
        )
        self.assertEqual(result, {
            "league_id": 77,
            "year": 2026,
            "drafted": False,
            "picks": [],
            "message": "This league has not completed a draft yet for the selected year.",
        })

    def test_missing_active_player_name_preserves_espn_api_empty_string_behavior(self):
        draft = {
            "draftDetail": {
                "drafted": True,
                "picks": [{"roundId": 1, "roundPickNumber": 1, "playerId": 999, "teamId": 1}],
            },
            "teams": [{"id": 1, "name": "One"}],
        }
        pick = build_draft_results(draft, [], 1, 2026)["picks"][0]
        self.assertEqual(pick["player_name"], "")

    def test_unknown_team_ids_match_wrapper_none_behavior(self):
        draft = {
            "draftDetail": {
                "drafted": True,
                "picks": [{"roundId": 1, "roundPickNumber": 1, "playerId": 1, "teamId": 99,
                           "nominatingTeamId": 98}],
            },
            "teams": [{"id": 1, "name": "Known"}],
        }
        pick = build_draft_results(draft, [{"id": 1, "fullName": "P"}], 1, 2026)["picks"][0]
        self.assertIsNone(pick["team_id"])
        self.assertIsNone(pick["team_name"])
        self.assertIsNone(pick["nominating_team_id"])
        self.assertIsNone(pick["nominating_team_name"])

    def test_malformed_shapes_fail_closed(self):
        with self.assertRaises(ESPNDraftPayloadError):
            build_draft_results([], [], 1, 2026)
        with self.assertRaises(ESPNDraftPayloadError):
            build_draft_results({"draftDetail": {"drafted": True, "picks": {}}}, [], 1, 2026)
        with self.assertRaises(ESPNDraftPayloadError):
            build_draft_results({"draftDetail": {"drafted": True, "picks": []}}, {}, 1, 2026)


if __name__ == "__main__":
    unittest.main()
