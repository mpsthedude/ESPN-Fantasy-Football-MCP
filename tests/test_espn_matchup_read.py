import unittest

from espn_matchup_read import (
    ESPNMatchupPayloadError,
    build_matchup_info,
    current_scoring_week,
    matchup_period_for_week,
    resolve_matchup_request,
    valid_scoring_weeks,
)


def context_payload():
    return {
        "scoringPeriodId": 16,
        "status": {
            "currentMatchupPeriod": 15,
            "firstScoringPeriod": 1,
            "finalScoringPeriod": 18,
        },
        "settings": {
            "scheduleSettings": {
                "matchupPeriods": {
                    "1": [1],
                    "2": [2],
                    "15": [15, 16],
                    "17": [17, 18],
                }
            }
        },
    }


class ESPNMatchupReadTests(unittest.TestCase):
    def test_valid_weeks_come_from_matchup_periods(self):
        self.assertEqual(valid_scoring_weeks(context_payload()), [1, 2, 15, 16, 17, 18])

    def test_valid_weeks_fall_back_to_status_range(self):
        payload = {
            "status": {"firstScoringPeriod": 1, "finalScoringPeriod": 4},
            "settings": {"scheduleSettings": {}},
        }
        self.assertEqual(valid_scoring_weeks(payload), [1, 2, 3, 4])

    def test_current_week_is_capped_at_final_scoring_period(self):
        payload = {
            "scoringPeriodId": 21,
            "status": {"finalScoringPeriod": 18},
        }
        self.assertEqual(current_scoring_week(payload), 18)

    def test_multi_week_playoff_scoring_period_maps_to_matchup_period(self):
        payload = context_payload()
        self.assertEqual(matchup_period_for_week(payload, 16), 15)
        self.assertEqual(matchup_period_for_week(payload, 18), 17)

    def test_resolve_defaults_to_current_week_and_rejects_invalid_values(self):
        payload = context_payload()
        self.assertEqual(resolve_matchup_request(payload, None), (16, 15, [1, 2, 15, 16, 17, 18]))
        self.assertEqual(resolve_matchup_request(payload, 18), (18, 17, [1, 2, 15, 16, 17, 18]))
        self.assertEqual(resolve_matchup_request(payload, 14), (None, None, [1, 2, 15, 16, 17, 18]))
        self.assertEqual(resolve_matchup_request(payload, True), (None, None, [1, 2, 15, 16, 17, 18]))

    def test_matchup_output_preserves_legacy_shape_and_bye_behavior(self):
        payload = {
            "teams": [
                {"id": 1, "name": "Alpha"},
                {"id": 2, "location": "Beta", "nickname": "Bears"},
                {"id": 3, "name": "Gamma"},
            ],
            "schedule": [
                {
                    "home": {"teamId": 1, "totalPoints": 101.5},
                    "away": {"teamId": 2, "totalPoints": 99.25},
                },
                {
                    "home": {"teamId": 3, "totalPoints": 87.0},
                },
            ],
        }
        result = build_matchup_info(payload, 16)
        self.assertEqual(result, [
            {
                "home_team": "Alpha",
                "home_score": 101.5,
                "away_team": "Beta Bears",
                "away_score": 99.25,
                "winner": "HOME",
            },
            {
                "home_team": "Gamma",
                "home_score": 87.0,
                "away_team": "BYE",
                "away_score": 0,
                "winner": "HOME",
            },
        ])

    def test_tie_and_away_winner_semantics_match_legacy_tool(self):
        payload = {
            "teams": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
            "schedule": [
                {"home": {"teamId": 1, "totalPoints": 10}, "away": {"teamId": 2, "totalPoints": 10}},
                {"home": {"teamId": 1, "totalPoints": 5}, "away": {"teamId": 2, "totalPoints": 6}},
            ],
        }
        result = build_matchup_info(payload, 1)
        self.assertEqual([row["winner"] for row in result], ["TIE", "AWAY"])

    def test_missing_schedule_fails_closed(self):
        with self.assertRaises(ESPNMatchupPayloadError):
            build_matchup_info({"teams": []}, 1)


if __name__ == "__main__":
    unittest.main()
