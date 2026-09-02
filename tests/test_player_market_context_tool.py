import unittest
from unittest.mock import patch

import player_market_context_tools as context_tools


class FakeClient:
    def __init__(self):
        self.calls = []

    def sportsbook_player_props(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "player": {"playerID": "p1", "name": "Bo Example", "position": "QB"},
            "bookmakers": ["draftkings", "fanduel"],
            "events": [
                {
                    "eventID": "evt-1",
                    "startsAt": "2026-09-06T17:00:00Z",
                    "teams": {},
                    "props": [
                        {
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


class FakeTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def fetch_league(self, league_id, year, views):
        self.calls.append({"league_id": league_id, "year": year, "views": views})
        return self.payload


class PlayerMarketContextToolTests(unittest.TestCase):
    @patch.object(context_tools.fp_client, "get_cache_freshness_report")
    @patch.object(context_tools.fp_client, "build_player_intelligence")
    def test_context_uses_one_logical_sgo_call_and_zero_live_fp_calls(self, build_intel, freshness):
        build_intel.return_value = {
            "match_method": "name_position_only",
            "match_confidence": "medium",
            "name": "Bo Example",
            "team": "DEN",
            "position": "QB",
            "ecr": 9,
            "rank_min": 6,
            "rank_max": 14,
            "rank_std": 2.1,
            "injury_status": None,
            "recent_news": [],
        }
        freshness.return_value = {"players": {"status": "fresh"}}
        client = FakeClient()

        result = context_tools._get_player_prop_market_context(
            client,
            event_id="evt-1",
            player_name="Bo Example",
            league="NFL",
            team_id="team-den",
            stat_id="passing_yards",
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["event_id"], "evt-1")
        self.assertEqual(client.calls[0]["limit"], 1)
        self.assertEqual(result["providerCost"]["fantasyProsLiveRequests"], 0)
        self.assertEqual(result["providerCost"]["espnRosterReads"], 0)
        self.assertFalse(result["providerCost"]["sportsGameOdds"]["hiddenPagination"])
        self.assertEqual(result["sportsbook"]["summary"]["marketsWithDisagreement"], 1)
        build_intel.assert_called_once_with("Bo Example", team=None, position="QB", scoring="PPR")

    def test_non_nfl_and_invalid_scoring_fail_before_sgo_call(self):
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "supports NFL only"):
            context_tools._get_player_prop_market_context(
                client,
                event_id="evt-1",
                player_name="Player",
                league="NBA",
                team_id="team-1",
            )
        self.assertEqual(client.calls, [])

        with self.assertRaisesRegex(ValueError, "scoring must be one of"):
            context_tools._get_player_prop_market_context(
                client,
                event_id="evt-1",
                player_name="Player",
                league="NFL",
                team_id="team-1",
                scoring="banana",
            )
        self.assertEqual(client.calls, [])

    def test_espn_year_without_league_fails_before_sgo_call(self):
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "espn_year requires espn_league_id"):
            context_tools._get_player_prop_market_context(
                client,
                event_id="evt-1",
                player_name="Player",
                league="NFL",
                team_id="team-1",
                espn_year=2026,
            )
        self.assertEqual(client.calls, [])

    def test_espn_context_makes_one_roster_read(self):
        payload = {
            "teams": [
                {
                    "id": 1,
                    "name": "Example Team",
                    "roster": {
                        "entries": [
                            {
                                "lineupSlotId": 0,
                                "playerPoolEntry": {
                                    "player": {
                                        "id": 7,
                                        "fullName": "Bo Example",
                                        "eligibleSlots": [0],
                                        "defaultPositionId": 1,
                                        "proTeamId": 7,
                                        "injured": True,
                                        "injuryStatus": "QUESTIONABLE",
                                        "stats": [],
                                    }
                                },
                            }
                        ]
                    },
                }
            ]
        }
        transport = FakeTransport(payload)
        with patch.object(context_tools.api, "get_transport", return_value=transport):
            result = context_tools._espn_player_context("Bo Example", 123, 2026)

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0]["league_id"], 123)
        self.assertEqual(result["status"], "matched")
        self.assertTrue(result["player"]["injured"])


if __name__ == "__main__":
    unittest.main()
