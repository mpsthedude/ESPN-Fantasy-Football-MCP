import unittest

from sportsgameodds_disagreement import rank_slate_market_disagreements


BOOKS = ["draftkings", "fanduel", "betmgm"]


def spread_event(event_id, home_lines, *, starts_at="2026-09-06T17:00:00Z"):
    by_book = {}
    for book, (line, odds) in zip(BOOKS, home_lines):
        by_book[book] = {
            "available": True,
            "spread": line,
            "odds": odds,
        }
    return {
        "eventID": event_id,
        "sportID": "FOOTBALL",
        "leagueID": "NFL",
        "status": {"startsAt": starts_at},
        "teams": {"home": {"names": {"long": f"Home {event_id}"}}},
        "odds": {
            "points-home-game-sp-home": {
                "oddID": "points-home-game-sp-home",
                "marketName": "Game Spread",
                "statID": "points",
                "periodID": "game",
                "betTypeID": "sp",
                "sideID": "home",
                "byBookmaker": by_book,
            }
        },
    }


def moneyline_event(event_id, prices):
    by_book = {}
    for book, odds in zip(BOOKS, prices):
        by_book[book] = {"available": True, "odds": odds}
    return {
        "eventID": event_id,
        "sportID": "FOOTBALL",
        "leagueID": "NFL",
        "status": {"startsAt": "2026-09-06T17:00:00Z"},
        "teams": {},
        "odds": {
            "points-home-game-ml-home": {
                "oddID": "points-home-game-ml-home",
                "marketName": "Moneyline",
                "statID": "points",
                "periodID": "game",
                "betTypeID": "ml",
                "sideID": "home",
                "byBookmaker": by_book,
            }
        },
    }


class SportsbookDisagreementTests(unittest.TestCase):
    def test_spread_ranking_prioritizes_line_range_before_same_line_price(self):
        slate = {
            "bookmakers": BOOKS,
            "events": [
                spread_event("line-move", [(-2.5, -110), (-3, 100), (-2.5, -105)]),
                spread_event("price-only", [(-3, -130), (-3, 120), (-3, -110)]),
            ],
            "nextCursor": "opaque-next",
        }

        result = rank_slate_market_disagreements(slate, market="spread")

        self.assertEqual([row["event"]["eventID"] for row in result["results"]], ["line-move", "price-only"])
        self.assertEqual(result["results"][0]["maxPostedLineSpread"], 0.5)
        self.assertEqual(result["results"][1]["maxPostedLineSpread"], 0)
        self.assertGreater(result["results"][1]["maxSameLinePriceProbabilitySpread"], 0)
        self.assertEqual(result["nextCursor"], "opaque-next")
        self.assertIn("posted-line range first", result["rankingBasis"])

    def test_moneyline_ranking_uses_implied_probability_spread(self):
        slate = {
            "bookmakers": BOOKS,
            "events": [
                moneyline_event("wide", [-150, 130, -120]),
                moneyline_event("tight", [-115, -110, -105]),
            ],
        }

        result = rank_slate_market_disagreements(slate, market="moneyline")

        self.assertEqual(result["results"][0]["event"]["eventID"], "wide")
        self.assertGreater(
            result["results"][0]["maxImpliedProbabilitySpread"],
            result["results"][1]["maxImpliedProbabilitySpread"],
        )
        self.assertIn("implied-probability spread", result["rankingBasis"])

    def test_consensus_rows_and_single_book_rows_are_not_reported(self):
        two_books = spread_event("consensus", [(-3, -110), (-3, -110), (-3, -110)])
        one_book = spread_event("one-book", [(-2.5, -110), (-3, 100), (-2.5, -105)])
        one_book["odds"]["points-home-game-sp-home"]["byBookmaker"] = {
            "draftkings": {"available": True, "spread": -2.5, "odds": -110}
        }
        slate = {"bookmakers": BOOKS, "events": [two_books, one_book]}

        result = rank_slate_market_disagreements(slate, market="spread", min_bookmakers=2)

        self.assertEqual(result["results"], [])
        self.assertEqual(result["eventsScanned"], 2)
        self.assertEqual(result["marketsWithOffers"], 2)

    def test_player_prop_is_rejected_from_game_slate_path(self):
        with self.assertRaisesRegex(ValueError, "player-prop disagreement path"):
            rank_slate_market_disagreements({"events": []}, market="player_prop")


if __name__ == "__main__":
    unittest.main()
