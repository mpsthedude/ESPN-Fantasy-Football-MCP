import ast
import asyncio
import inspect
import textwrap
import unittest
from unittest.mock import patch

import espn_fantasy_server as espn


class FakeDraftTransport:
    def __init__(self):
        self.league_calls = []
        self.player_calls = []

    def fetch_league(self, league_id, year, *, views=None, scoring_period_id=None, fantasy_filter=None):
        self.league_calls.append({
            "league_id": league_id,
            "year": year,
            "views": tuple(views or ()),
            "scoring_period_id": scoring_period_id,
            "fantasy_filter": fantasy_filter,
        })
        return {
            "draftDetail": {
                "drafted": True,
                "picks": [{
                    "roundId": 1,
                    "roundPickNumber": 1,
                    "playerId": 101,
                    "teamId": 3,
                    "keeper": False,
                }],
            },
            "teams": [{"id": 3, "name": "Test Team"}],
        }

    def fetch_players(self, year, *, views=None, fantasy_filter=None):
        self.player_calls.append({
            "year": year,
            "views": tuple(views or ()),
            "fantasy_filter": fantasy_filter,
        })
        return [{"id": 101, "fullName": "Test Player"}]


class ESPNPhaseCDraftResultsTests(unittest.TestCase):
    def test_get_draft_results_uses_project_transport_and_preserves_contract(self):
        transport = FakeDraftTransport()
        with patch.object(espn.api, "get_transport", return_value=transport):
            result = asyncio.run(espn.get_draft_results(55, 2026))

        self.assertTrue(result["drafted"])
        self.assertEqual(result["pick_count"], 1)
        self.assertEqual(result["picks"][0]["player_name"], "Test Player")
        self.assertEqual(result["picks"][0]["team_name"], "Test Team")
        self.assertEqual(transport.league_calls[0]["views"], ("mDraftDetail", "mTeam"))
        self.assertEqual(transport.player_calls[0]["views"], ("players_wl",))
        self.assertEqual(transport.player_calls[0]["fantasy_filter"], {"filterActive": {"value": True}})

    def test_get_draft_results_source_has_no_wrapper_league_call(self):
        source = textwrap.dedent(inspect.getsource(espn.get_draft_results))
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        wrapper_calls = [
            node for node in calls
            if isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_league"
        ]
        self.assertEqual(wrapper_calls, [])


if __name__ == "__main__":
    unittest.main()
