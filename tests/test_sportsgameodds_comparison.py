import unittest

from sportsgameodds_comparison import (
    build_game_market_comparison,
    build_player_prop_comparison,
    game_market_odd_ids,
    normalize_comparison_market,
)


class SportsGameOddsComparisonTests(unittest.TestCase):
    def test_market_aliases_and_fixed_game_odd_ids(self):
        self.assertEqual(normalize_comparison_market("ML"), "moneyline")
        self.assertEqual(normalize_comparison_market("over/under"), "total")
        self.assertEqual(normalize_comparison_market("player_prop"), "player_prop")
        self.assertEqual(
            game_market_odd_ids("spread"),
            ("points-home-game-sp-home", "points-away-game-sp-away"),
        )
        with self.assertRaises(ValueError):
            normalize_comparison_market("first touchdown")

    def test_moneyline_comparison_identifies_best_posted_price(self):
        payload = {
            "success": True,
            "data": [{
                "eventID": "GAME-1",
                "sportID": "FOOTBALL",
                "leagueID": "NFL",
                "status": {"startsAt": "2026-09-10T20:20:00Z"},
                "teams": {"home": {"teamID": "DEN"}, "away": {"teamID": "KC"}},
                "odds": {
                    "points-home-game-ml-home": {
                        "marketName": "Moneyline",
                        "statID": "points",
                        "statEntityID": "home",
                        "periodID": "game",
                        "betTypeID": "ml",
                        "sideID": "home",
                        "fairOdds": "-115",
                        "bookOdds": "-120",
                        "byBookmaker": {
                            "draftkings": {"odds": "-110", "available": True},
                            "fanduel": {"odds": "-105", "available": True},
                            "closedbook": {"odds": "+100", "available": False},
                        },
                    },
                    "points-away-game-ml-away": {
                        "marketName": "Moneyline",
                        "statID": "points",
                        "statEntityID": "away",
                        "periodID": "game",
                        "betTypeID": "ml",
                        "sideID": "away",
                        "fairOdds": "+105",
                        "bookOdds": "+100",
                        "byBookmaker": {
                            "draftkings": {"odds": "+102", "available": True},
                            "fanduel": {"odds": "+100", "available": True},
                        },
                    },
                },
            }],
        }

        result = build_game_market_comparison(
            payload,
            market="moneyline",
            bookmakers_requested=("draftkings", "fanduel"),
            event_id="GAME-1",
        )

        self.assertEqual(result["scope"], "game")
        self.assertEqual(result["event"]["eventID"], "GAME-1")
        sides = {side["sideID"]: side for side in result["markets"][0]["sides"]}
        self.assertEqual(sides["home"]["bookmakerCount"], 2)
        self.assertEqual(sides["home"]["bestPostedPrice"]["bookmakerID"], "fanduel")
        self.assertEqual(sides["home"]["bestPostedPrice"]["odds"], "-105")
        self.assertIsNone(sides["home"]["mostFavorablePostedLine"])
        self.assertAlmostEqual(sides["home"]["bestPostedPrice"]["impliedProbability"], 0.512195, places=6)

    def test_spread_comparison_keeps_line_and_price_separate(self):
        payload = {
            "data": [{
                "eventID": "GAME-2",
                "status": {},
                "odds": {
                    "points-home-game-sp-home": {
                        "marketName": "Spread",
                        "statID": "points",
                        "statEntityID": "home",
                        "periodID": "game",
                        "betTypeID": "sp",
                        "sideID": "home",
                        "fairOdds": "-110",
                        "fairSpread": "-2.5",
                        "bookOdds": "-110",
                        "bookSpread": "-3",
                        "byBookmaker": {
                            "draftkings": {"spread": "-3", "odds": "-105", "available": True},
                            "fanduel": {"spread": "-2.5", "odds": "-125", "available": True},
                            "betmgm": {"spread": "-3", "odds": "+100", "available": True},
                        },
                    },
                    "points-away-game-sp-away": {
                        "marketName": "Spread",
                        "statID": "points",
                        "statEntityID": "away",
                        "periodID": "game",
                        "betTypeID": "sp",
                        "sideID": "away",
                        "byBookmaker": {
                            "draftkings": {"spread": "+3", "odds": "-115", "available": True},
                            "fanduel": {"spread": "+2.5", "odds": "+105", "available": True},
                        },
                    },
                },
            }],
        }

        result = build_game_market_comparison(payload, market="spread", event_id="GAME-2")
        sides = {side["sideID"]: side for side in result["markets"][0]["sides"]}
        home = sides["home"]
        self.assertIsNone(home["bestPostedPrice"], "different lines must not be collapsed into one best price")
        self.assertEqual(home["mostFavorablePostedLine"], -2.5)
        self.assertEqual(home["lineRange"], {"min": -3, "max": -2.5, "spread": 0.5})
        groups = {group["line"]: group for group in home["lineGroups"]}
        self.assertEqual(groups[-3]["bestPriceOffer"]["bookmakerID"], "betmgm")
        self.assertEqual(groups[-3]["bestPriceOffer"]["odds"], "+100")
        self.assertEqual(sides["away"]["mostFavorablePostedLine"], 3)

    def test_total_side_favorability_uses_lower_over_and_higher_under(self):
        payload = {
            "data": [{
                "eventID": "GAME-3",
                "status": {},
                "odds": {
                    "points-all-game-ou-over": {
                        "marketName": "Total",
                        "statID": "points",
                        "statEntityID": "all",
                        "periodID": "game",
                        "betTypeID": "ou",
                        "sideID": "over",
                        "byBookmaker": {
                            "a": {"overUnder": "47.5", "odds": "-110", "available": True},
                            "b": {"overUnder": "48", "odds": "+100", "available": True},
                        },
                    },
                    "points-all-game-ou-under": {
                        "marketName": "Total",
                        "statID": "points",
                        "statEntityID": "all",
                        "periodID": "game",
                        "betTypeID": "ou",
                        "sideID": "under",
                        "byBookmaker": {
                            "a": {"overUnder": "47.5", "odds": "-110", "available": True},
                            "b": {"overUnder": "48", "odds": "-120", "available": True},
                        },
                    },
                },
            }],
        }
        result = build_game_market_comparison(payload, market="total")
        sides = {side["sideID"]: side for side in result["markets"][0]["sides"]}
        self.assertEqual(sides["over"]["mostFavorablePostedLine"], 47.5)
        self.assertEqual(sides["under"]["mostFavorablePostedLine"], 48)

    def test_game_comparison_tolerates_missing_or_malformed_nested_market_data(self):
        payload = {
            "data": {
                "GAME-4": {
                    "eventID": "GAME-4",
                    "status": "drifted",
                    "odds": {
                        "points-home-game-ml-home": {
                            "betTypeID": "ml",
                            "sideID": "home",
                            "byBookmaker": "drifted",
                        },
                        "points-away-game-ml-away": "not-a-market",
                    },
                }
            }
        }
        result = build_game_market_comparison(payload, market="moneyline", event_id="GAME-4")
        self.assertEqual(result["event"]["eventID"], "GAME-4")
        self.assertEqual(len(result["markets"]), 1)
        self.assertEqual(result["markets"][0]["sides"][0]["bookmakerCount"], 0)

    def test_player_prop_comparison_groups_identical_lines_and_prices(self):
        snapshot = {
            "player": {"playerID": "BO_NIX_NFL", "name": "Bo Nix", "teamID": "DEN"},
            "bookmakersRequested": ["draftkings", "fanduel", "betmgm"],
            "events": [{
                "eventID": "GAME-5",
                "startsAt": "2026-09-13T20:25:00Z",
                "teams": {},
                "markets": [{
                    "statID": "passing_yards",
                    "marketName": "Bo Nix Passing Yards Over/Under",
                    "betTypeID": "ou",
                    "periodID": "game",
                    "fairLine": 247.5,
                    "fairPrices": {"over": "-102", "under": "-108"},
                    "bookmakers": {
                        "draftkings": {"line": 247.5, "prices": {"over": "-110", "under": "-110"}},
                        "fanduel": {"line": 245.5, "prices": {"over": "-115", "under": "-105"}},
                        "betmgm": {"line": 247.5, "prices": {"over": "+100", "under": "-120"}},
                    },
                }],
            }],
        }

        result = build_player_prop_comparison(
            snapshot, stat_id="passing_yards", bet_type="ou", event_id="GAME-5"
        )
        self.assertEqual(result["scope"], "player_prop")
        sides = {side["sideID"]: side for side in result["markets"][0]["sides"]}
        self.assertEqual(sides["over"]["mostFavorablePostedLine"], 245.5)
        self.assertEqual(sides["under"]["mostFavorablePostedLine"], 247.5)
        over_groups = {group["line"]: group for group in sides["over"]["lineGroups"]}
        self.assertEqual(over_groups[247.5]["bestPriceOffer"]["bookmakerID"], "betmgm")
        self.assertEqual(over_groups[247.5]["bestPriceOffer"]["odds"], "+100")

    def test_player_prop_comparison_can_filter_bet_type(self):
        snapshot = {
            "events": [{
                "eventID": "GAME-6",
                "markets": [
                    {"statID": "touchdowns", "betTypeID": "yn", "periodID": "game", "bookmakers": {}},
                    {"statID": "touchdowns", "betTypeID": "other", "periodID": "game", "bookmakers": {}},
                ],
            }]
        }
        result = build_player_prop_comparison(snapshot, stat_id="touchdowns", bet_type="yn")
        self.assertEqual(len(result["markets"]), 1)
        self.assertEqual(result["markets"][0]["betTypeID"], "yn")


if __name__ == "__main__":
    unittest.main()
