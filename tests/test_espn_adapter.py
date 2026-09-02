"""Deterministic contract tests for espn_adapter.build_espn_league_snapshot.

Standard library only (unittest + types.SimpleNamespace). No new package
dependency introduced. Run with: python -m unittest discover
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from espn_adapter import build_espn_league_snapshot, build_espn_teams
from fantasy_models import FantasyPlayer, FantasyTeam, LeagueSnapshot


def make_player(name=None, position=None, proTeam=None, projected_total_points=None, total_points=None, extra_attrs=None):
    ns = SimpleNamespace(name=name, position=position, proTeam=proTeam,
                          projected_total_points=projected_total_points, total_points=total_points)
    if extra_attrs:
        for k, v in extra_attrs.items():
            setattr(ns, k, v)
    return ns


def make_team(team_id, team_name, roster):
    return SimpleNamespace(team_id=team_id, team_name=team_name, roster=roster)


def make_league(teams):
    return SimpleNamespace(teams=teams)


class TestBuildEspnLeagueSnapshot(unittest.TestCase):
    def setUp(self):
        self.p1 = make_player(name="Matthew Stafford", position="QB", proTeam="LAR",
                               projected_total_points=399.9, total_points=0.0)
        self.p2 = make_player(name="A.J. Brown", position="WR", proTeam="NE",
                               projected_total_points=270.8, total_points=0.0)
        self.team1 = make_team(1, "Team One", [self.p1, self.p2])
        self.team2 = make_team(7, "FURIOUS LIME", [])
        self.league = make_league([self.team1, self.team2])
        self.slot_counts = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "RB/WR/TE": 2}
        self.snapshot = build_espn_league_snapshot(
            league=self.league, league_id=123456789, year=2026,
            slot_counts=self.slot_counts, scoring_bucket="PPR")

    def test_league_id_preserved(self):
        self.assertEqual(self.snapshot.league_id, 123456789)

    def test_year_preserved(self):
        self.assertEqual(self.snapshot.year, 2026)

    def test_platform_is_espn(self):
        self.assertEqual(self.snapshot.platform, "espn")

    def test_scoring_bucket_preserved(self):
        self.assertEqual(self.snapshot.scoring_bucket, "PPR")

    def test_roster_slot_counts_preserved(self):
        self.assertEqual(self.snapshot.roster_slot_counts, self.slot_counts)

    def test_team_id_preserved_with_original_type(self):
        self.assertEqual(self.snapshot.teams[0].team_id, 1)
        self.assertIsInstance(self.snapshot.teams[0].team_id, int)

    def test_team_name_preserved(self):
        self.assertEqual(self.snapshot.teams[0].team_name, "Team One")

    def test_player_name_preserved(self):
        self.assertEqual(self.snapshot.teams[0].roster[0].name, "Matthew Stafford")

    def test_position_preserved(self):
        self.assertEqual(self.snapshot.teams[0].roster[0].position, "QB")

    def test_proteam_maps_to_pro_team(self):
        self.assertEqual(self.snapshot.teams[0].roster[0].pro_team, "LAR")

    def test_projected_total_points_maps_to_season_projected_points(self):
        self.assertEqual(self.snapshot.teams[0].roster[0].season_projected_points, 399.9)

    def test_total_points_maps_to_season_total_points(self):
        self.assertEqual(self.snapshot.teams[0].roster[0].season_total_points, 0.0)

    def test_missing_optional_player_attributes_become_none(self):
        bare = make_player()
        team = make_team(9, "Bare Team", [bare])
        league = make_league([team])
        snap = build_espn_league_snapshot(league=league, league_id=1, year=2026,
                                           slot_counts={}, scoring_bucket="PPR")
        player = snap.teams[0].roster[0]
        self.assertIsNone(player.name)
        self.assertIsNone(player.position)
        self.assertIsNone(player.pro_team)
        self.assertIsNone(player.season_projected_points)
        self.assertIsNone(player.season_total_points)

    def test_multiple_teams_and_players_preserve_order_and_count(self):
        self.assertEqual(len(self.snapshot.teams), 2)
        self.assertEqual(len(self.snapshot.teams[0].roster), 2)
        self.assertEqual(self.snapshot.teams[0].roster[0].name, "Matthew Stafford")
        self.assertEqual(self.snapshot.teams[0].roster[1].name, "A.J. Brown")
        self.assertEqual(self.snapshot.teams[1].team_id, 7)
        self.assertEqual(len(self.snapshot.teams[1].roster), 0)


class TestModelContractAllowsStringIds(unittest.TestCase):
    """Model-contract test only. No Yahoo objects or Yahoo behavior added.
    Proves FantasyId permits a string without coercion, even though this
    adapter currently only ever receives ESPN integer IDs."""

    def test_league_snapshot_accepts_string_league_id(self):
        snap = LeagueSnapshot(platform="hypothetical", league_id="abc-123", year=2026,
                               scoring_bucket="PPR", roster_slot_counts={}, teams=[])
        self.assertEqual(snap.league_id, "abc-123")
        self.assertIsInstance(snap.league_id, str)

    def test_fantasy_team_accepts_string_team_id(self):
        team = FantasyTeam(team_id="team-42", team_name="Compound Key Team", roster=[])
        self.assertEqual(team.team_id, "team-42")
        self.assertIsInstance(team.team_id, str)

    def test_espn_adapter_still_preserves_int_id_type(self):
        league = make_league([make_team(3, "T", [])])
        snap = build_espn_league_snapshot(league=league, league_id=123456789, year=2026,
                                           slot_counts={}, scoring_bucket="PPR")
        self.assertIsInstance(snap.teams[0].team_id, int)


class TestBuildEspnTeams(unittest.TestCase):
    """Deterministic contract tests for the generalized, lower-level
    build_espn_teams() extraction (Prompt 3B)."""

    def setUp(self):
        self.p1 = make_player(name="Matthew Stafford", position="QB", proTeam="LAR",
                               projected_total_points=399.9, total_points=0.0)
        self.p2 = make_player(name="A.J. Brown", position="WR", proTeam="NE",
                               projected_total_points=270.8, total_points=0.0)
        self.p3 = make_player(name="Bijan Robinson", position="RB", proTeam="ATL",
                               projected_total_points=386.7, total_points=0.0)
        self.team1 = make_team(1, "Team One", [self.p1, self.p2])
        self.team2 = make_team(7, "FURIOUS LIME", [self.p3])
        self.league = make_league([self.team1, self.team2])
        self.teams = build_espn_teams(self.league)

    def test_multiple_teams_preserved(self):
        self.assertEqual(len(self.teams), 2)

    def test_multiple_players_preserved(self):
        self.assertEqual(len(self.teams[0].roster), 2)
        self.assertEqual(len(self.teams[1].roster), 1)

    def test_ordering_preserved(self):
        self.assertEqual(self.teams[0].team_id, 1)
        self.assertEqual(self.teams[1].team_id, 7)
        self.assertEqual(self.teams[0].roster[0].name, "Matthew Stafford")
        self.assertEqual(self.teams[0].roster[1].name, "A.J. Brown")

    def test_team_ids_preserved_without_coercion(self):
        self.assertIsInstance(self.teams[0].team_id, int)
        self.assertEqual(self.teams[0].team_id, 1)

    def test_names_preserved(self):
        self.assertEqual(self.teams[1].roster[0].name, "Bijan Robinson")

    def test_proteam_mapping_preserved(self):
        self.assertEqual(self.teams[0].roster[0].pro_team, "LAR")

    def test_season_projection_mapping_preserved(self):
        self.assertEqual(self.teams[0].roster[0].season_projected_points, 399.9)

    def test_total_points_mapping_preserved(self):
        self.assertEqual(self.teams[0].roster[0].season_total_points, 0.0)

    def test_missing_optional_attributes_become_none(self):
        bare = make_player()
        team = make_team(9, "Bare Team", [bare])
        league = make_league([team])
        teams = build_espn_teams(league)
        player = teams[0].roster[0]
        self.assertIsNone(player.name)
        self.assertIsNone(player.position)
        self.assertIsNone(player.pro_team)
        self.assertIsNone(player.season_projected_points)
        self.assertIsNone(player.season_total_points)

    def test_snapshot_matches_direct_build_espn_teams(self):
        """build_espn_league_snapshot must produce the exact same
        normalized team/player values as calling build_espn_teams directly
        -- proves the internal deduplication introduced no drift."""
        snapshot = build_espn_league_snapshot(
            league=self.league, league_id=123456789, year=2026,
            slot_counts={"QB": 1}, scoring_bucket="PPR")
        direct_teams = build_espn_teams(self.league)
        self.assertEqual(snapshot.teams, direct_teams)


if __name__ == "__main__":
    unittest.main()
