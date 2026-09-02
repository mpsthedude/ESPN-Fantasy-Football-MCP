import unittest

from sportsgameodds_tools import _generic_player_props, _generic_slate, _normalize_league


class FakeClient:
    def __init__(self, slate_result=None, props_result=None):
        self.slate_result = slate_result or {"leagueID": None, "sportID": None, "events": []}
        self.props_result = props_result or {"player": None, "events": []}
        self.slate_calls = []
        self.props_calls = []

    def sportsbook_slate(self, **params):
        self.slate_calls.append(params)
        return self.slate_result

    def sportsbook_player_props(self, **params):
        self.props_calls.append(params)
        return self.props_result


class MultiSportSportsbookTests(unittest.TestCase):
    def test_normalize_common_league_aliases(self):
        self.assertEqual(_normalize_league("cfb"), "NCAAF")
        self.assertEqual(_normalize_league("college football"), "NCAAF")
        self.assertEqual(_normalize_league("cbb"), "NCAAB")
        self.assertEqual(_normalize_league("nba"), "NBA")

    def test_generic_slate_normalizes_ncaaf_and_delegates_to_client(self):
        expected = {
            "leagueID": "NCAAF",
            "sportID": None,
            "bookmakers": ["draftkings", "fanduel"],
            "events": [{"eventID": "game-1", "leagueID": "NCAAF"}],
        }
        client = FakeClient(slate_result=expected)

        result = _generic_slate(client, league="CFB", bookmakers="draftkings,fanduel", limit=10)

        self.assertIs(result, expected)
        self.assertEqual(client.slate_calls, [{
            "league": "NCAAF",
            "sport": None,
            "team_id": None,
            "bookmakers": "draftkings,fanduel",
            "starts_after": None,
            "starts_before": None,
            "cursor": None,
            "limit": 10,
        }])

    def test_generic_slate_forwards_team_id_unchanged(self):
        client = FakeClient()
        _generic_slate(
            client, league="NFL", team_id="DENVER_BRONCOS_NFL", bookmakers="draftkings"
        )
        self.assertEqual(client.slate_calls[0]["team_id"], "DENVER_BRONCOS_NFL")
        self.assertEqual(client.slate_calls[0]["league"], "NFL")

    def test_generic_slate_forwards_date_window_unchanged(self):
        client = FakeClient()
        _generic_slate(
            client,
            league="NCAAF",
            starts_after="2026-09-05T00:00:00-04:00",
            starts_before="2026-09-06T00:00:00-04:00",
            limit=25,
        )
        self.assertEqual(client.slate_calls[0]["starts_after"], "2026-09-05T00:00:00-04:00")
        self.assertEqual(client.slate_calls[0]["starts_before"], "2026-09-06T00:00:00-04:00")

    def test_generic_slate_forwards_cursor_unchanged(self):
        client = FakeClient()
        _generic_slate(
            client, sport="football", bookmakers="draftkings",
            cursor="opaque+/cursor==", limit=25,
        )
        self.assertEqual(client.slate_calls[0]["cursor"], "opaque+/cursor==")
        self.assertEqual(client.slate_calls[0]["sport"], "FOOTBALL")

    def test_generic_slate_accepts_sport_scope(self):
        client = FakeClient()
        _generic_slate(client, sport="college football", limit=5)
        self.assertEqual(client.slate_calls[0]["sport"], "COLLEGE_FOOTBALL")
        self.assertIsNone(client.slate_calls[0]["league"])

    def test_generic_slate_requires_exactly_one_scope(self):
        client = FakeClient()
        with self.assertRaises(ValueError):
            _generic_slate(client)
        with self.assertRaises(ValueError):
            _generic_slate(client, league="NFL", sport="FOOTBALL")
        self.assertEqual(client.slate_calls, [])

    def test_generic_player_props_delegates_then_compacts_non_nfl_market(self):
        raw = {
            "player": {
                "playerID": "STAR_PLAYER_1_NBA",
                "name": "Star Player",
                "teamID": "TEAM_ONE_NBA",
                "position": "PG",
            },
            "leagueID": "NBA",
            "requestedStatID": None,
            "bookmakers": ["draftkings"],
            "events": [{
                "eventID": "nba-game-1",
                "sportID": "BASKETBALL",
                "leagueID": "NBA",
                "startsAt": "2026-10-20T23:00:00.000Z",
                "teams": {"home": {"teamID": "TEAM_ONE_NBA"}, "away": {"teamID": "TEAM_TWO_NBA"}},
                "props": [
                    {
                        "oddID": "points-over",
                        "marketName": "Star Player Points Over/Under",
                        "statID": "points",
                        "periodID": "game",
                        "betTypeID": "ou",
                        "sideID": "over",
                        "fairOdds": "-105",
                        "fairOverUnder": "24.5",
                        "byBookmaker": {
                            "draftkings": {"odds": "-110", "overUnder": "24.5", "available": True}
                        },
                    },
                    {
                        "oddID": "points-under",
                        "marketName": "Star Player Points Over/Under",
                        "statID": "points",
                        "periodID": "game",
                        "betTypeID": "ou",
                        "sideID": "under",
                        "fairOdds": "+105",
                        "fairOverUnder": "24.5",
                        "byBookmaker": {
                            "draftkings": {"odds": "-110", "overUnder": "24.5", "available": True}
                        },
                    },
                ],
            }],
        }
        client = FakeClient(props_result=raw)

        result = _generic_player_props(
            client,
            player_name="Star Player",
            league="nba",
            team_id="TEAM_ONE_NBA",
            bookmakers="draftkings",
        )

        self.assertEqual(client.props_calls, [{
            "player_name": "Star Player",
            "league": "NBA",
            "team_id": "TEAM_ONE_NBA",
            "event_id": None,
            "stat_id": None,
            "bookmakers": "draftkings",
            "include_alt_lines": False,
            "limit": 4,
        }])
        market = result["events"][0]["markets"][0]
        self.assertEqual(market["statID"], "points")
        self.assertEqual(market["consensusLine"], 24.5)
        self.assertEqual(market["bookmakers"]["draftkings"]["prices"], {"over": "-110", "under": "-110"})

    def test_generic_player_props_forwards_event_id_unchanged(self):
        client = FakeClient(props_result={"player": None, "events": []})
        _generic_player_props(
            client,
            player_name="Star Player",
            league="NBA",
            team_id="TEAM_ONE_NBA",
            event_id="GAME-123",
        )
        self.assertEqual(client.props_calls[0]["event_id"], "GAME-123")

    def test_generic_player_props_rejects_empty_team_before_client_call(self):
        client = FakeClient()
        with self.assertRaises(ValueError):
            _generic_player_props(client, player_name="Star Player", league="NBA", team_id="")
        self.assertEqual(client.props_calls, [])


if __name__ == "__main__":
    unittest.main()
