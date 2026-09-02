"""Deterministic tool-level tests for get_all_rosters().

No live ESPN calls. Verifies the response schema/keys remain exactly compatible
while the tool reads raw ESPN mTeam/mRoster payloads through the project-owned
transport/parser boundary.
"""
import asyncio
import unittest
from unittest.mock import patch

from espn_reference import PRO_TEAM_MAP

import espn_fantasy_server as srv


PRO_TEAM_ID = next(team_id for team_id, name in PRO_TEAM_MAP.items() if name == "LAR")


def make_player_entry(name=None, position_slot=0, lineup_slot=0,
                      projected_total_points=None, total_points=None):
    stats = []
    if total_points is not None:
        stats.append({
            "seasonId": 2026, "scoringPeriodId": 0, "statSourceId": 0,
            "statSplitTypeId": 0, "appliedTotal": total_points,
            "appliedAverage": 0, "stats": {}, "appliedStats": {},
        })
    if projected_total_points is not None:
        stats.append({
            "seasonId": 2026, "scoringPeriodId": 0, "statSourceId": 1,
            "statSplitTypeId": 0, "appliedTotal": projected_total_points,
            "appliedAverage": 0, "stats": {}, "appliedStats": {},
        })
    return {
        "lineupSlotId": lineup_slot,
        "playerPoolEntry": {
            "player": {
                "fullName": name,
                "eligibleSlots": [position_slot] if position_slot is not None else [],
                "defaultPositionId": position_slot,
                "proTeamId": PRO_TEAM_ID if name is not None else None,
                "stats": stats,
            }
        },
    }


def make_team(team_id, team_name, roster, wins=0, losses=0):
    return {
        "id": team_id,
        "name": team_name,
        "record": {"overall": {"wins": wins, "losses": losses}},
        "roster": {"entries": roster},
    }


def make_payload(teams):
    return {"members": [], "teams": teams}


def run(coro):
    return asyncio.run(coro)


class TestGetAllRostersDetailedFalse(unittest.TestCase):
    def setUp(self):
        self.player = make_player_entry(
            name="Matthew Stafford", position_slot=0, lineup_slot=0,
            projected_total_points=399.9, total_points=12.5,
        )
        self.payload = make_payload([make_team(1, "Team One", [self.player], wins=5, losses=2)])

    def test_response_fields(self):
        with patch.object(srv, "_fetch_roster_payload", return_value=self.payload):
            result = run(srv.get_all_rosters(123456789, detailed=False))
        self.assertNotIn("error", result)
        team_out = result["teams"][0]
        player_out = team_out["roster"][0]

        self.assertEqual(team_out["team_id"], 1)
        self.assertEqual(team_out["team_name"], "Team One")
        self.assertEqual(team_out["wins"], 5)
        self.assertEqual(team_out["losses"], 2)

        self.assertEqual(player_out["name"], "Matthew Stafford")
        self.assertEqual(player_out["position"], "QB")
        self.assertEqual(player_out["proTeam"], "LAR")
        self.assertEqual(player_out["projected_points"], 399.9)
        self.assertEqual(player_out["points"], 12.5)
        self.assertEqual(player_out["lineup_slot"], "QB")

    def test_stats_not_emitted_when_detailed_false(self):
        with patch.object(srv, "_fetch_roster_payload", return_value=self.payload):
            result = run(srv.get_all_rosters(123456789, detailed=False))
        self.assertNotIn("stats", result["teams"][0]["roster"][0])


class TestGetAllRostersDetailedTrue(unittest.TestCase):
    def test_stats_emitted_when_detailed_true(self):
        player = make_player_entry(
            name="A.J. Brown", position_slot=4, lineup_slot=4,
            projected_total_points=270.8, total_points=5.0,
        )
        payload = make_payload([make_team(1, "Team One", [player], wins=5, losses=2)])
        with patch.object(srv, "_fetch_roster_payload", return_value=payload):
            result = run(srv.get_all_rosters(123456789, detailed=True))
        player_out = result["teams"][0]["roster"][0]
        self.assertIn("stats", player_out)
        self.assertEqual(player_out["projected_points"], 270.8)
        self.assertEqual(player_out["points"], 5.0)
        self.assertEqual(player_out["lineup_slot"], "WR")


class TestGetAllRostersMultipleAndOrdering(unittest.TestCase):
    def test_multiple_teams_and_players_ordering_preserved(self):
        p1 = make_player_entry("Player A", position_slot=2, lineup_slot=2,
                               projected_total_points=100.0, total_points=1.0)
        p2 = make_player_entry("Player B", position_slot=4, lineup_slot=4,
                               projected_total_points=200.0, total_points=2.0)
        p3 = make_player_entry("Player C", position_slot=6, lineup_slot=6,
                               projected_total_points=300.0, total_points=3.0)
        payload = make_payload([
            make_team(3, "Team Three", [p1, p2], wins=1, losses=1),
            make_team(7, "Team Seven", [p3], wins=2, losses=0),
        ])

        with patch.object(srv, "_fetch_roster_payload", return_value=payload):
            result = run(srv.get_all_rosters(123456789, detailed=False))

        self.assertEqual(result["team_count"], 2)
        self.assertEqual(result["teams"][0]["team_id"], 3)
        self.assertEqual(result["teams"][1]["team_id"], 7)
        self.assertEqual([p["name"] for p in result["teams"][0]["roster"]], ["Player A", "Player B"])
        self.assertEqual(result["teams"][1]["roster"][0]["name"], "Player C")

    def test_response_keys_remain_exactly_compatible(self):
        p1 = make_player_entry("Player A", position_slot=2, lineup_slot=2,
                               projected_total_points=100.0, total_points=1.0)
        payload = make_payload([make_team(3, "Team Three", [p1], wins=1, losses=1)])

        with patch.object(srv, "_fetch_roster_payload", return_value=payload):
            result = run(srv.get_all_rosters(123456789, detailed=False))

        self.assertEqual(set(result.keys()), {"league_id", "year", "team_count", "teams"})
        self.assertEqual(set(result["teams"][0].keys()), {"team_id", "team_name", "wins", "losses", "roster"})
        self.assertEqual(
            set(result["teams"][0]["roster"][0].keys()),
            {"name", "position", "proTeam", "projected_points", "points", "lineup_slot"},
        )


if __name__ == "__main__":
    unittest.main()
