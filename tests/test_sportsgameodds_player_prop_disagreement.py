import unittest

from sportsgameodds_disagreement import rank_player_prop_disagreements


class SportsbookPlayerPropDisagreementTests(unittest.TestCase):
    def _snapshot(self):
        return {
            "player": {"playerID": "p1", "name": "Bo Example"},
            "bookmakersRequested": ["draftkings", "fanduel", "betmgm"],
            "events": [
                {
                    "eventID": "evt-1",
                    "startsAt": "2026-09-06T17:00:00Z",
                    "teams": {},
                    "markets": [
                        {
                            "statID": "passing_yards",
                            "marketName": "Passing Yards",
                            "betTypeID": "ou",
                            "periodID": "game",
                            "fairLine": 251.5,
                            "fairPrices": {"over": -110, "under": -110},
                            "bookmakers": {
                                "draftkings": {"line": 250.5, "prices": {"over": -110, "under": -110}},
                                "fanduel": {"line": 252.5, "prices": {"over": 100, "under": -120}},
                                "betmgm": {"line": 250.5, "prices": {"over": -105, "under": -115}},
                            },
                        },
                        {
                            "statID": "passing_touchdowns",
                            "marketName": "Passing Touchdowns",
                            "betTypeID": "ou",
                            "periodID": "game",
                            "fairLine": 1.5,
                            "fairPrices": {"over": -110, "under": -110},
                            "bookmakers": {
                                "draftkings": {"line": 1.5, "prices": {"over": -135, "under": 110}},
                                "fanduel": {"line": 1.5, "prices": {"over": 120, "under": -145}},
                                "betmgm": {"line": 1.5, "prices": {"over": -105, "under": -115}},
                            },
                        },
                        {
                            "statID": "touchdowns",
                            "marketName": "Anytime Touchdown",
                            "betTypeID": "yn",
                            "periodID": "game",
                            "fairLine": None,
                            "fairPrices": {"yes": 120, "no": -150},
                            "bookmakers": {
                                "draftkings": {"line": None, "prices": {"yes": 130, "no": -160}},
                                "fanduel": {"line": None, "prices": {"yes": 105, "no": -135}},
                                "betmgm": {"line": None, "prices": {"yes": 125, "no": -150}},
                            },
                        },
                    ],
                }
            ],
        }

    def test_groups_rank_within_bet_type_without_cross_type_score(self):
        result = rank_player_prop_disagreements(self._snapshot(), top_n=10)

        self.assertEqual(set(result["groups"]), {"ou", "yn"})
        self.assertEqual(result["groups"]["ou"][0]["statID"], "passing_yards")
        self.assertEqual(result["groups"]["ou"][0]["maxPostedLineSpread"], 2)
        self.assertEqual(result["groups"]["yn"][0]["statID"], "touchdowns")
        self.assertGreater(result["groups"]["yn"][0]["maxImpliedProbabilitySpread"], 0)
        self.assertIn("posted-line range first", result["rankingBasis"]["ou"])
        self.assertIn("implied-probability spread", result["rankingBasis"]["yn"])

    def test_stat_and_bet_type_filters_narrow_markets_before_ranking(self):
        result = rank_player_prop_disagreements(
            self._snapshot(),
            stat_id="passing_touchdowns",
            bet_type="ou",
        )

        self.assertEqual(set(result["groups"]), {"ou"})
        self.assertEqual(len(result["groups"]["ou"]), 1)
        self.assertEqual(result["groups"]["ou"][0]["statID"], "passing_touchdowns")
        self.assertEqual(result["marketsScanned"], 1)


if __name__ == "__main__":
    unittest.main()
