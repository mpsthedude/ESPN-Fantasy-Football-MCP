import unittest

from player_market_context import build_player_market_context


class PlayerMarketContextTests(unittest.TestCase):
    def test_combines_market_fp_and_espn_evidence_without_edge_score(self):
        disagreement = {
            "eventID": "evt-1",
            "leagueID": "NFL",
            "teamID": "DEN",
            "playerName": "Bo Nix",
            "groups": {
                "ou": [
                    {
                        "statID": "passing_yards",
                        "maxPostedLineSpread": 4.5,
                        "maxSameLinePriceProbabilitySpreadPctPts": 3.2,
                        "maxImpliedProbabilitySpreadPctPts": 5.1,
                    }
                ]
            },
        }
        fantasypros = {
            "match_method": "exact_name_team_position",
            "match_confidence": "high",
            "name": "Bo Nix",
            "team": "DEN",
            "position": "QB",
            "ecr": 8,
            "pos_rank": "QB8",
            "tier": 3,
            "rank_min": 5,
            "rank_max": 14,
            "rank_std": 2.4,
            "adp": 72.3,
            "projected_points": 318.7,
            "injury_status": "Questionable",
            "injury_comment": "Limited in practice.",
            "recent_news": [
                {"created": "2026-09-02", "title": "Nix limited", "impact": "Monitor practice participation"}
            ],
            "cache_timestamps": {"players": "2026-09-02T12:00:00Z"},
        }
        espn = {
            "status": "matched",
            "leagueID": 123,
            "year": 2026,
            "player": {
                "name": "Bo Nix",
                "position": "QB",
                "team": "DEN",
                "points": 0,
                "projected_points": 305.4,
                "injured": True,
            },
        }
        freshness = {
            "players": {"status": "fresh"},
            "rankings_QB": {"status": "fresh"},
            "projections_QB": {"status": "stale"},
            "injuries": {"status": "fresh"},
        }

        result = build_player_market_context(
            disagreement,
            fantasypros,
            scoring="PPR",
            espn=espn,
            fantasypros_freshness=freshness,
        )

        self.assertEqual(result["sportsbook"]["summary"]["marketsWithDisagreement"], 1)
        self.assertEqual(result["sportsbook"]["summary"]["maxPostedLineSpread"], 4.5)
        self.assertEqual(result["fantasyPros"]["matchConfidence"], "high")
        self.assertEqual(result["espn"]["status"], "matched")
        signal_types = {signal["type"] for signal in result["explanatorySignals"]}
        self.assertIn("cross_book_market_disagreement", signal_types)
        self.assertIn("fantasypros_injury_context", signal_types)
        self.assertIn("fantasypros_expert_rank_dispersion", signal_types)
        self.assertIn("recent_player_news", signal_types)
        self.assertIn("espn_injury_flag", signal_types)
        self.assertIn("cross_source_injury_corroboration", signal_types)
        self.assertEqual(result["dataQuality"]["fantasyProsStaleOrMissingDatasets"], ["projections_QB"])
        serialized = repr(result).lower()
        self.assertNotIn("expectedvalue", serialized)
        self.assertNotIn("recommendedwager", serialized)
        self.assertIn("does not calculate expected value", result["interpretation"].lower())

    def test_unresolved_fp_match_and_no_espn_remain_explicit(self):
        result = build_player_market_context(
            {
                "eventID": "evt-2",
                "leagueID": "NFL",
                "teamID": "DEN",
                "playerName": "Unknown Player",
                "groups": {},
            },
            {
                "match_method": "no_match",
                "match_confidence": "none",
                "candidates": [],
            },
        )

        self.assertEqual(result["fantasyPros"]["matchConfidence"], "none")
        self.assertEqual(result["espn"]["status"], "not_requested")
        self.assertEqual(result["explanatorySignals"], [])
        self.assertEqual(result["dataQuality"]["sportsbookMarketsWithDisagreement"], 0)

    def test_rank_dispersion_is_raw_evidence_not_threshold_score(self):
        result = build_player_market_context(
            {"groups": {}},
            {
                "match_method": "name_only_single_candidate",
                "match_confidence": "medium",
                "name": "Player",
                "rank_min": 10,
                "rank_max": 11,
                "rank_std": 0.3,
            },
        )

        signal = next(s for s in result["explanatorySignals"] if s["type"] == "fantasypros_expert_rank_dispersion")
        self.assertEqual(signal["evidence"]["rankRangeWidth"], 1.0)
        self.assertIn("no threshold", signal["whyItMayMatter"].lower())


if __name__ == "__main__":
    unittest.main()
