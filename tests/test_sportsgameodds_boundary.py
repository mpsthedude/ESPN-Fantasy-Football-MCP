import unittest
from unittest.mock import patch

from sportsgameodds_client import SportsGameOddsClient


class SportsGameOddsBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.client = SportsGameOddsClient(api_key="synthetic-test-key")

    def test_nfl_slate_delegates_to_generic_client_and_preserves_legacy_shape(self):
        generic = {
            "leagueID": "NFL",
            "sportID": None,
            "bookmakers": ["draftkings"],
            "events": [{
                "eventID": "game-1",
                "sportID": "FOOTBALL",
                "leagueID": "NFL",
                "status": {"startsAt": "2026-09-10T00:00:00Z"},
                "startsAt": "2026-09-10T00:00:00Z",
                "teams": {"home": {"teamID": "DENVER_BRONCOS_NFL"}},
                "odds": {"points-home-game-ml-home": {"oddID": "points-home-game-ml-home"}},
            }],
            "nextCursor": "next-1",
            "notice": "notice",
            "interpretation": "generic interpretation",
        }

        with patch.object(self.client, "sportsbook_slate", return_value=generic) as delegated:
            result = self.client.nfl_slate(bookmakers=("draftkings",), limit=7)

        delegated.assert_called_once_with(
            league="NFL",
            bookmakers=("draftkings",),
            limit=7,
        )
        self.assertEqual(result, {
            "leagueID": "NFL",
            "bookmakers": ["draftkings"],
            "events": [{
                "eventID": "game-1",
                "status": {"startsAt": "2026-09-10T00:00:00Z"},
                "startsAt": "2026-09-10T00:00:00Z",
                "teams": {"home": {"teamID": "DENVER_BRONCOS_NFL"}},
                "odds": {"points-home-game-ml-home": {"oddID": "points-home-game-ml-home"}},
            }],
            "nextCursor": "next-1",
            "notice": "notice",
        })

    def test_nfl_player_props_delegates_to_generic_client_without_leagueid_leak(self):
        generic = {
            "player": {
                "playerID": "BO_NIX_NFL",
                "name": "Bo Nix",
                "position": "QB",
                "teamID": "DENVER_BRONCOS_NFL",
            },
            "leagueID": "NFL",
            "requestedStatID": "passing_yards",
            "bookmakers": ["draftkings"],
            "includeAltLines": False,
            "events": [{"eventID": "game-1", "props": []}],
            "notice": None,
        }

        with patch.object(self.client, "sportsbook_player_props", return_value=generic) as delegated:
            result = self.client.nfl_player_props(
                player_name="Bo Nix",
                team="DEN",
                stat_id="passing_yards",
                bookmakers=("draftkings",),
                include_alt_lines=False,
                limit=3,
            )

        delegated.assert_called_once_with(
            player_name="Bo Nix",
            league="NFL",
            team_id="DENVER_BRONCOS_NFL",
            stat_id="passing_yards",
            bookmakers=("draftkings",),
            include_alt_lines=False,
            limit=3,
        )
        self.assertNotIn("leagueID", result)
        self.assertEqual(result["player"]["name"], "Bo Nix")
        self.assertEqual(generic["leagueID"], "NFL", "delegation must not mutate canonical result")


if __name__ == "__main__":
    unittest.main()
