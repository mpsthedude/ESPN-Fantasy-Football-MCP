import unittest

from sportsgameodds_analysis import compact_player_prop_snapshot


class SportsGameOddsAnalysisTests(unittest.TestCase):
    def test_compacts_and_pairs_available_full_game_markets(self):
        raw = {
            "player": {"playerID": "BO_NIX_1_NFL", "name": "Bo Nix", "teamID": "DENVER_BRONCOS_NFL"},
            "bookmakers": ["draftkings", "fanduel", "caesars"],
            "events": [
                {
                    "eventID": "game-1",
                    "startsAt": "2026-09-15T00:15:00.000Z",
                    "teams": {"home": {"teamID": "KC"}, "away": {"teamID": "DEN"}},
                    "props": [
                        {
                            "marketName": "Bo Nix Rushing Yards Over/Under",
                            "statID": "rushing_yards",
                            "periodID": "game",
                            "betTypeID": "ou",
                            "sideID": "over",
                            "fairOdds": "-104",
                            "fairOverUnder": "16.5",
                            "byBookmaker": {
                                "fanduel": {"odds": "-114", "overUnder": "15.5", "available": True, "lastUpdatedAt": "2026-08-31T22:25:48.947Z"},
                                "draftkings": {"odds": "-117", "overUnder": "16.5", "available": True, "lastUpdatedAt": "2026-08-31T22:25:55.447Z"},
                                "caesars": {"odds": "+108", "overUnder": "19.5", "available": True, "lastUpdatedAt": "2026-08-31T22:25:18.947Z"},
                            },
                        },
                        {
                            "marketName": "Bo Nix Rushing Yards Over/Under",
                            "statID": "rushing_yards",
                            "periodID": "game",
                            "betTypeID": "ou",
                            "sideID": "under",
                            "fairOdds": "+104",
                            "fairOverUnder": "16.5",
                            "byBookmaker": {
                                "fanduel": {"odds": "-114", "overUnder": "15.5", "available": True, "lastUpdatedAt": "2026-08-31T22:25:48.947Z"},
                                "draftkings": {"odds": "-107", "overUnder": "16.5", "available": True, "lastUpdatedAt": "2026-08-31T22:25:55.447Z"},
                            },
                        },
                        {
                            "marketName": "Bo Nix 1st Quarter Passing Yards Over/Under",
                            "statID": "passing_yards",
                            "periodID": "1q",
                            "betTypeID": "ou",
                            "sideID": "over",
                            "byBookmaker": {"fanduel": {"odds": "-110", "overUnder": "47.5", "available": True}},
                        },
                        {
                            "marketName": "Bo Nix Interceptions Over/Under",
                            "statID": "defense_interceptions",
                            "periodID": "game",
                            "betTypeID": "ou",
                            "sideID": "over",
                            "byBookmaker": {"fanduel": {"odds": "-110", "overUnder": "0.5", "available": True}},
                        },
                        {
                            "marketName": "Bo Nix Passing Attempts Over/Under",
                            "statID": "passing_attempts",
                            "periodID": "game",
                            "betTypeID": "ou",
                            "sideID": "over",
                            "byBookmaker": {"fanduel": {"odds": "-102", "overUnder": "35.5", "available": False}},
                        },
                    ],
                }
            ],
        }

        result = compact_player_prop_snapshot(raw)
        self.assertEqual(len(result["events"]), 1)
        markets = result["events"][0]["markets"]
        self.assertEqual(len(markets), 1)
        market = markets[0]
        self.assertEqual(market["statID"], "rushing_yards")
        self.assertEqual(market["fairLine"], 16.5)
        self.assertEqual(market["consensusLine"], 16.5)
        self.assertEqual(market["bookmakers"]["fanduel"]["line"], 15.5)
        self.assertEqual(market["bookmakers"]["fanduel"]["prices"], {"over": "-114", "under": "-114"})
        self.assertEqual(market["bookmakers"]["draftkings"]["prices"], {"over": "-117", "under": "-107"})
        self.assertEqual(market["bookmakers"]["caesars"]["prices"], {"over": "+108"})

    def test_keeps_yes_no_touchdown_market(self):
        raw = {
            "player": {"name": "Bo Nix"},
            "events": [{
                "eventID": "game-1",
                "props": [{
                    "marketName": "Bo Nix Any Touchdowns Yes/No",
                    "statID": "touchdowns",
                    "periodID": "game",
                    "betTypeID": "yn",
                    "sideID": "yes",
                    "fairOdds": "+530",
                    "byBookmaker": {
                        "draftkings": {"odds": "+500", "available": True, "lastUpdatedAt": "2026-08-31T22:53:08.423Z"}
                    },
                }],
            }],
        }
        result = compact_player_prop_snapshot(raw)
        market = result["events"][0]["markets"][0]
        self.assertEqual(market["statID"], "touchdowns")
        self.assertEqual(market["bookmakers"]["draftkings"]["prices"]["yes"], "+500")
        self.assertIsNone(market["consensusLine"])


if __name__ == "__main__":
    unittest.main()
