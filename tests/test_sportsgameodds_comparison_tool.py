import unittest

from sportsgameodds_tools import _compare_sportsbook_market


class FakeClient:
    def __init__(self, *, event_payload=None, props_payload=None):
        self.event_payload = event_payload or {"data": []}
        self.props_payload = props_payload or {"player": None, "events": []}
        self.event_calls = []
        self.props_calls = []

    def events(self, **params):
        self.event_calls.append(params)
        return self.event_payload

    def sportsbook_player_props(self, **params):
        self.props_calls.append(params)
        return self.props_payload


class SportsGameOddsComparisonToolBoundaryTests(unittest.TestCase):
    def test_game_comparison_uses_one_exact_targeted_event_call(self):
        client = FakeClient(event_payload={
            "data": [{
                "eventID": "GAME-1",
                "leagueID": "NFL",
                "status": {},
                "odds": {
                    "points-home-game-ml-home": {
                        "marketName": "Moneyline",
                        "statID": "points",
                        "statEntityID": "home",
                        "periodID": "game",
                        "betTypeID": "ml",
                        "sideID": "home",
                        "byBookmaker": {"draftkings": {"odds": "-110", "available": True}},
                    },
                    "points-away-game-ml-away": {
                        "marketName": "Moneyline",
                        "statID": "points",
                        "statEntityID": "away",
                        "periodID": "game",
                        "betTypeID": "ml",
                        "sideID": "away",
                        "byBookmaker": {"draftkings": {"odds": "+100", "available": True}},
                    },
                },
            }]
        })

        result = _compare_sportsbook_market(
            client,
            event_id="  GAME-1  ",
            league="nfl",
            market="moneyline",
            bookmakers="draftkings,fanduel",
        )

        self.assertEqual(len(client.event_calls), 1)
        self.assertEqual(client.props_calls, [])
        call = client.event_calls[0]
        self.assertEqual(call["eventID"], "GAME-1")
        self.assertEqual(call["leagueID"], "NFL")
        self.assertEqual(call["limit"], 1)
        self.assertEqual(call["oddsAvailable"], "true")
        self.assertEqual(call["bookmakerID"], "draftkings,fanduel")
        self.assertIn("points-home-game-ml-home", call["oddID"])
        self.assertIn("points-away-game-ml-away", call["oddID"])
        self.assertEqual(result["event"]["eventID"], "GAME-1")

    def test_player_prop_comparison_reuses_exact_event_prop_boundary(self):
        client = FakeClient(props_payload={
            "player": {"playerID": "PLAYER-1", "name": "Star Player", "teamID": "TEAM-1"},
            "bookmakers": ["draftkings", "fanduel"],
            "events": [{
                "eventID": "GAME-2",
                "startsAt": "2026-10-20T23:00:00Z",
                "teams": {},
                "props": [
                    {
                        "marketName": "Star Player Points Over/Under",
                        "statID": "points",
                        "periodID": "game",
                        "betTypeID": "ou",
                        "sideID": "over",
                        "fairOdds": "-105",
                        "fairOverUnder": "24.5",
                        "byBookmaker": {
                            "draftkings": {"odds": "-110", "overUnder": "24.5", "available": True},
                            "fanduel": {"odds": "+100", "overUnder": "23.5", "available": True},
                        },
                    },
                    {
                        "marketName": "Star Player Points Over/Under",
                        "statID": "points",
                        "periodID": "game",
                        "betTypeID": "ou",
                        "sideID": "under",
                        "fairOdds": "-105",
                        "fairOverUnder": "24.5",
                        "byBookmaker": {
                            "draftkings": {"odds": "-110", "overUnder": "24.5", "available": True},
                            "fanduel": {"odds": "-120", "overUnder": "23.5", "available": True},
                        },
                    },
                ],
            }],
        })

        result = _compare_sportsbook_market(
            client,
            event_id="GAME-2",
            league="nba",
            market="player_prop",
            bookmakers="draftkings,fanduel",
            player_name="Star Player",
            team_id="TEAM-1",
            stat_id="points",
            bet_type="ou",
        )

        self.assertEqual(client.event_calls, [])
        self.assertEqual(len(client.props_calls), 1)
        call = client.props_calls[0]
        self.assertEqual(call["event_id"], "GAME-2")
        self.assertEqual(call["league"], "NBA")
        self.assertEqual(call["team_id"], "TEAM-1")
        self.assertEqual(call["stat_id"], "points")
        self.assertFalse(call["include_alt_lines"])
        self.assertEqual(call["limit"], 1)
        self.assertEqual(result["scope"], "player_prop")
        self.assertEqual(result["requestedStatID"], "points")
        self.assertEqual(result["markets"][0]["betTypeID"], "ou")

    def test_player_prop_comparison_validates_required_scope_before_provider_call(self):
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "player_name is required"):
            _compare_sportsbook_market(
                client,
                event_id="GAME-3",
                league="NBA",
                market="player_prop",
                team_id="TEAM-1",
                stat_id="points",
            )
        self.assertEqual(client.event_calls, [])
        self.assertEqual(client.props_calls, [])

    def test_event_id_is_required_for_all_comparisons(self):
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "event_id is required"):
            _compare_sportsbook_market(client, event_id=" ", league="NFL", market="spread")
        self.assertEqual(client.event_calls, [])
        self.assertEqual(client.props_calls, [])


if __name__ == "__main__":
    unittest.main()
