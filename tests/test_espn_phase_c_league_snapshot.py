import ast
import asyncio
import inspect
import textwrap
import unittest
from unittest.mock import patch

import espn_fantasy_server as espn


class FakeSnapshotTransport:
    def __init__(self):
        self.league_calls = []
        self.season_calls = []

    @staticmethod
    def _base_payload():
        return {
            "seasonId": 2026,
            "scoringPeriodId": 3,
            "status": {"latestScoringPeriod": 3, "finalScoringPeriod": 17},
            "settings": {
                "name": "Snapshot Test",
                "size": 1,
                "scheduleSettings": {"playoffTeamCount": 1},
                "rosterSettings": {"lineupSlotCounts": {"0": 1}},
                "scoringSettings": {"scoringType": "H2H_POINTS", "scoringItems": []},
            },
            "draftDetail": {"drafted": True, "picks": [{"roundId": 1}]},
            "teams": [{
                "id": 1,
                "name": "Test Team",
                "rankCalculatedFinal": 0,
                "playoffSeed": 1,
                "record": {"overall": {"wins": 2, "losses": 0, "pointsFor": 200, "pointsAgainst": 150}},
                "roster": {"entries": [{
                    "lineupSlotId": 0,
                    "playerPoolEntry": {"player": {
                        "fullName": "Roster QB",
                        "eligibleSlots": [0],
                        "proTeamId": 2,
                        "stats": [{
                            "seasonId": 2026,
                            "statSplitTypeId": 0,
                            "scoringPeriodId": 0,
                            "statSourceId": 1,
                            "appliedTotal": 300,
                            "appliedAverage": 17.6,
                            "stats": {},
                            "appliedStats": {},
                        }],
                    }},
                }]},
            }],
            "schedule": [],
        }

    def fetch_league(self, league_id, year, *, views=None, scoring_period_id=None, fantasy_filter=None):
        call = {
            "league_id": league_id,
            "year": year,
            "views": tuple(views or ()),
            "scoring_period_id": scoring_period_id,
            "fantasy_filter": fantasy_filter,
        }
        self.league_calls.append(call)
        if "kona_player_info" in call["views"]:
            return {
                "players": [{
                    "player": {
                        "fullName": "Free Agent",
                        "eligibleSlots": [2, 23],
                        "proTeamId": 1,
                        "injured": False,
                        "stats": [{
                            "seasonId": 2026,
                            "statSplitTypeId": 0,
                            "scoringPeriodId": 3,
                            "statSourceId": 1,
                            "appliedTotal": 12.34,
                        }],
                    }
                }]
            }
        return self._base_payload()

    def fetch_season(self, year, *, views=None):
        self.season_calls.append({"year": year, "views": tuple(views or ())})
        return {
            "settings": {
                "proTeams": [{
                    "id": 1,
                    "proGamesByScoringPeriod": {
                        "3": [{"awayProTeamId": 1, "homeProTeamId": 2}]
                    },
                }, {
                    "id": 2,
                    "proGamesByScoringPeriod": {
                        "3": [{"awayProTeamId": 1, "homeProTeamId": 2}]
                    },
                }],
            }
        }


class ESPNPhaseCLeagueSnapshotTests(unittest.TestCase):
    def test_snapshot_uses_project_transport_and_preserves_composite_contract(self):
        transport = FakeSnapshotTransport()
        with patch.object(espn.api, "get_transport", return_value=transport):
            result = asyncio.run(espn.get_league_snapshot(55, 2026, 1))

        self.assertEqual(result["league_name"], "Snapshot Test")
        self.assertEqual(result["current_week"], 3)
        self.assertEqual(result["standings"][0]["team_id"], 1)
        self.assertEqual(result["rosters"][0]["roster"][0]["name"], "Roster QB")
        self.assertTrue(result["draft_completed"])
        self.assertEqual(result["free_agents_week_used"], 3)
        self.assertTrue(result["free_agents_available"])
        self.assertIsNone(result["free_agents_error"])
        self.assertEqual(result["free_agents_top"][0]["name"], "Free Agent")
        self.assertEqual(result["free_agents_top"][0]["projected_points"], 12.34)

        self.assertEqual(transport.league_calls[0]["views"],
                         ("mTeam", "mMatchup", "mSettings", "mStandings", "mRoster", "mDraftDetail"))
        self.assertEqual(transport.league_calls[1]["views"], ("kona_player_info",))
        self.assertEqual(transport.season_calls[0]["views"], ("proTeamSchedules_wl",))

    def test_zero_free_agent_limit_skips_player_and_schedule_reads(self):
        transport = FakeSnapshotTransport()
        with patch.object(espn.api, "get_transport", return_value=transport):
            result = asyncio.run(espn.get_league_snapshot(55, 2026, 0))

        self.assertFalse(result["free_agents_available"])
        self.assertEqual(result["free_agents_top"], [])
        self.assertEqual(len(transport.league_calls), 1)
        self.assertEqual(transport.season_calls, [])

    def test_snapshot_source_has_no_wrapper_league_call(self):
        source = textwrap.dedent(inspect.getsource(espn.get_league_snapshot))
        tree = ast.parse(source)
        wrapper_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_league"
        ]
        self.assertEqual(wrapper_calls, [])


if __name__ == "__main__":
    unittest.main()
