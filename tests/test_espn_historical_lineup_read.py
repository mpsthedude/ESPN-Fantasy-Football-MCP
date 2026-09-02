import unittest

from espn_reference import POSITION_MAP

from espn_historical_lineup_read import (
    ESPNHistoricalLineupPayloadError,
    build_historical_lineup_boxes,
)


class ESPNHistoricalLineupReadTests(unittest.TestCase):
    def _pro_schedule(self):
        return {
            "settings": {
                "proTeams": [
                    {"id": 0, "proGamesByScoringPeriod": {}},
                    {"id": 1, "proGamesByScoringPeriod": {"5": [{"id": 101}]}},
                    {"id": 2, "proGamesByScoringPeriod": {"5": [{"id": 102}]}},
                    {"id": 3, "proGamesByScoringPeriod": {"4": [{"id": 103}]}},
                ]
            }
        }

    def _entry(self, player_id, name, pro_team_id, *, slot_id=0, injury="ACTIVE", stats=None):
        return {
            "lineupSlotId": slot_id,
            "playerPoolEntry": {
                "player": {
                    "id": player_id,
                    "fullName": name,
                    "eligibleSlots": [0, 20],
                    "injuryStatus": injury,
                    "proTeamId": pro_team_id,
                    "stats": stats or [],
                }
            },
        }

    def test_builds_box_compatible_team_and_player_attributes(self):
        payload = {
            "schedule": [
                {
                    "home": {
                        "teamId": 10,
                        "rosterForCurrentScoringPeriod": {
                            "entries": [self._entry(1001, "Starter One", 1, injury="OUT")]
                        },
                    },
                    "away": {
                        "teamId": 20,
                        "rosterForCurrentScoringPeriod": {
                            "entries": [self._entry(2001, "Starter Two", 3, slot_id=2)]
                        },
                    },
                }
            ]
        }

        boxes = build_historical_lineup_boxes(payload, self._pro_schedule(), 5)

        self.assertEqual(len(boxes), 1)
        box = boxes[0]
        self.assertEqual(box.home_team.team_id, 10)
        self.assertEqual(box.away_team.team_id, 20)
        home = box.home_lineup[0]
        away = box.away_lineup[0]
        self.assertEqual(home.playerId, 1001)
        self.assertEqual(home.name, "Starter One")
        self.assertEqual(home.slot_position, POSITION_MAP[0])
        self.assertEqual(home.eligibleSlots, [POSITION_MAP[0], POSITION_MAP[20]])
        self.assertEqual(home.injuryStatus, "OUT")
        self.assertFalse(home.on_bye_week)
        self.assertTrue(away.on_bye_week)

    def test_prefers_historical_actual_stat_pro_team_like_boxplayer(self):
        payload = {
            "schedule": [
                {
                    "home": {
                        "teamId": 10,
                        "rosterForCurrentScoringPeriod": {
                            "entries": [
                                self._entry(
                                    1001,
                                    "Traded Player",
                                    3,
                                    stats=[
                                        {
                                            "scoringPeriodId": 5,
                                            "statSourceId": 0,
                                            "proTeamId": 2,
                                        }
                                    ],
                                )
                            ]
                        },
                    }
                }
            ]
        }

        boxes = build_historical_lineup_boxes(payload, self._pro_schedule(), 5)
        player = boxes[0].home_lineup[0]
        self.assertFalse(player.on_bye_week)

    def test_missing_away_side_preserves_bye_shape(self):
        payload = {
            "schedule": [
                {
                    "home": {
                        "teamId": 10,
                        "rosterForCurrentScoringPeriod": {"entries": []},
                    }
                }
            ]
        }
        box = build_historical_lineup_boxes(payload, self._pro_schedule(), 5)[0]
        self.assertEqual(box.home_team.team_id, 10)
        self.assertIsNone(box.away_team)
        self.assertEqual(box.away_lineup, [])

    def test_malformed_roster_fails_closed_instead_of_looking_clean(self):
        payload = {"schedule": [{"home": {"teamId": 10}}]}
        with self.assertRaises(ESPNHistoricalLineupPayloadError):
            build_historical_lineup_boxes(payload, self._pro_schedule(), 5)

    def test_week_validation_rejects_bool_and_nonpositive_values(self):
        for week in (True, 0, -1):
            with self.subTest(week=week):
                with self.assertRaises(ValueError):
                    build_historical_lineup_boxes({"schedule": []}, self._pro_schedule(), week)


if __name__ == "__main__":
    unittest.main()
