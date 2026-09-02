import inspect
import unittest

import espn_fantasy_server as server
from espn_matchup_read import build_commissioner_matchup_evidence


class CommissionerInvestigationDirectReadTests(unittest.TestCase):
    def test_matchup_evidence_preserves_ids_names_scores_and_schedule_order(self):
        payload = {
            "teams": [
                {"id": 3, "name": "Three"},
                {"id": 9, "location": "Nine", "nickname": "Niners"},
                {"id": 12, "name": "Twelve"},
            ],
            "schedule": [
                {"home": {"teamId": 3, "totalPoints": 101.25},
                 "away": {"teamId": 9, "totalPoints": 99.5}},
                {"home": {"teamId": 12, "totalPoints": 88.0},
                 "away": {"teamId": 3, "totalPoints": 90.0}},
            ],
        }
        row = build_commissioner_matchup_evidence(payload, 7, {9})
        self.assertEqual(row, {
            "week": 7,
            "source": "espn_scoreboard_week_7",
            "home_team_id": 3,
            "home_team_name": "Three",
            "home_score": 101.25,
            "away_team_id": 9,
            "away_team_name": "Nine Niners",
            "away_score": 99.5,
        })

    def test_matchup_evidence_mirrors_missing_away_placeholder(self):
        payload = {
            "teams": [{"id": 3, "name": "Three"}],
            "schedule": [{"home": {"teamId": 3, "totalPoints": 77.0}}],
        }
        row = build_commissioner_matchup_evidence(payload, 4, {3})
        self.assertEqual(row["away_team_id"], 0)
        self.assertIsNone(row["away_team_name"])
        self.assertEqual(row["away_score"], 0)

    def test_non_target_matchup_returns_none(self):
        payload = {
            "teams": [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}],
            "schedule": [{"home": {"teamId": 1, "totalPoints": 1},
                          "away": {"teamId": 2, "totalPoints": 2}}],
        }
        self.assertIsNone(build_commissioner_matchup_evidence(payload, 1, {99}))

    def test_public_investigation_has_no_wrapper_league_or_scoreboard_call(self):
        src = inspect.getsource(server.commissioner_investigate)
        self.assertNotIn("api.get_league(", src)
        self.assertNotIn("league.scoreboard(", src)
        self.assertIn("build_commissioner_snapshot", src)
        self.assertIn("build_commissioner_matchup_evidence", src)


if __name__ == "__main__":
    unittest.main()
