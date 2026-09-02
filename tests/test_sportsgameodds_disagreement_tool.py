import unittest

from sportsgameodds_disagreement_tools import _find_sportsbook_market_disagreements


class FakeClient:
    def __init__(self):
        self.calls = []

    def sportsbook_slate(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "leagueID": kwargs.get("league"),
            "sportID": kwargs.get("sport"),
            "bookmakers": ["draftkings", "fanduel"],
            "teamID": kwargs.get("team_id"),
            "startsAfter": kwargs.get("starts_after"),
            "startsBefore": kwargs.get("starts_before"),
            "events": [
                {
                    "eventID": "evt-1",
                    "sportID": "FOOTBALL",
                    "leagueID": "NFL",
                    "status": {"startsAt": "2026-09-06T17:00:00Z"},
                    "teams": {},
                    "odds": {
                        "points-home-game-sp-home": {
                            "oddID": "points-home-game-sp-home",
                            "marketName": "Game Spread",
                            "statID": "points",
                            "periodID": "game",
                            "betTypeID": "sp",
                            "sideID": "home",
                            "byBookmaker": {
                                "draftkings": {"available": True, "spread": -2.5, "odds": -110},
                                "fanduel": {"available": True, "spread": -3, "odds": 100},
                            },
                        }
                    },
                }
            ],
            "nextCursor": "next-page",
            "notice": None,
        }


class SportsbookDisagreementToolTests(unittest.TestCase):
    def test_one_call_fetches_one_slate_page_and_preserves_scope(self):
        client = FakeClient()

        result = _find_sportsbook_market_disagreements(
            client,
            market="spread",
            league="NFL",
            team_id="team-den",
            bookmakers="draftkings,fanduel",
            starts_after="2026-09-06T00:00:00-05:00",
            starts_before="2026-09-07T00:00:00-05:00",
            cursor="opaque-cursor",
            limit=12,
            top_n=5,
            min_bookmakers=2,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            client.calls[0],
            {
                "league": "NFL",
                "sport": None,
                "team_id": "team-den",
                "bookmakers": "draftkings,fanduel",
                "starts_after": "2026-09-06T00:00:00-05:00",
                "starts_before": "2026-09-07T00:00:00-05:00",
                "cursor": "opaque-cursor",
                "limit": 12,
            },
        )
        self.assertEqual(result["nextCursor"], "next-page")
        self.assertEqual(result["leagueID"], "NFL")
        self.assertEqual(result["teamID"], "team-den")
        self.assertEqual(result["results"][0]["event"]["eventID"], "evt-1")

    def test_scope_validation_happens_before_provider_call(self):
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            _find_sportsbook_market_disagreements(client, market="spread")
        self.assertEqual(client.calls, [])

    def test_player_props_are_rejected_before_provider_call(self):
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "player-prop disagreement path"):
            _find_sportsbook_market_disagreements(client, market="player_prop", league="NFL")
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
