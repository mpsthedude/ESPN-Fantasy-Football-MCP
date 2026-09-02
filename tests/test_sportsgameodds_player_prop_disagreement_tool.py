import unittest

from sportsgameodds_disagreement_tools import _find_sportsbook_player_prop_disagreements


class FakeClient:
    def __init__(self):
        self.calls = []

    def sportsbook_player_props(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "player": {"playerID": "p1", "name": "Bo Example"},
            "bookmakers": ["draftkings", "fanduel"],
            "events": [
                {
                    "eventID": "evt-1",
                    "startsAt": "2026-09-06T17:00:00Z",
                    "teams": {},
                    "props": [
                        {
                            "oddID": "passing-yards-over",
                            "marketName": "Passing Yards",
                            "statID": "passing_yards",
                            "periodID": "game",
                            "betTypeID": "ou",
                            "sideID": "over",
                            "fairOdds": -110,
                            "fairOverUnder": 251.5,
                            "byBookmaker": {
                                "draftkings": {"available": True, "overUnder": 250.5, "odds": -110},
                                "fanduel": {"available": True, "overUnder": 252.5, "odds": 100},
                            },
                        },
                        {
                            "oddID": "passing-yards-under",
                            "marketName": "Passing Yards",
                            "statID": "passing_yards",
                            "periodID": "game",
                            "betTypeID": "ou",
                            "sideID": "under",
                            "fairOdds": -110,
                            "fairOverUnder": 251.5,
                            "byBookmaker": {
                                "draftkings": {"available": True, "overUnder": 250.5, "odds": -110},
                                "fanduel": {"available": True, "overUnder": 252.5, "odds": -120},
                            },
                        },
                    ],
                }
            ],
            "notice": None,
        }


class SportsbookPlayerPropDisagreementToolTests(unittest.TestCase):
    def test_exact_event_prop_path_makes_one_bounded_client_call(self):
        client = FakeClient()

        result = _find_sportsbook_player_prop_disagreements(
            client,
            event_id="evt-1",
            player_name="Bo Example",
            league="NFL",
            team_id="team-den",
            stat_id="passing_yards",
            bookmakers="draftkings,fanduel",
            top_n=4,
            min_bookmakers=2,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            client.calls[0],
            {
                "player_name": "Bo Example",
                "league": "NFL",
                "team_id": "team-den",
                "event_id": "evt-1",
                "stat_id": "passing_yards",
                "bookmakers": "draftkings,fanduel",
                "include_alt_lines": False,
                "limit": 1,
            },
        )
        self.assertEqual(result["eventID"], "evt-1")
        self.assertEqual(result["leagueID"], "NFL")
        self.assertEqual(result["groups"]["ou"][0]["statID"], "passing_yards")

    def test_required_exact_scope_fails_before_provider_call(self):
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "event_id is required"):
            _find_sportsbook_player_prop_disagreements(
                client,
                event_id="",
                player_name="Bo Example",
                league="NFL",
                team_id="team-den",
            )
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
