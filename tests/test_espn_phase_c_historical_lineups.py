import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import espn_fantasy_server as espn


class FakeHistoricalTransport:
    def __init__(self):
        self.league_calls = []
        self.season_calls = []

    def fetch_league(self, league_id, year, *, views=None, scoring_period_id=None, fantasy_filter=None):
        self.league_calls.append({
            "league_id": league_id,
            "year": year,
            "views": tuple(views or ()),
            "scoring_period_id": scoring_period_id,
            "fantasy_filter": fantasy_filter,
        })
        return {
            "schedule": [
                {
                    "home": {
                        "teamId": 1,
                        "rosterForCurrentScoringPeriod": {
                            "entries": [
                                {
                                    "lineupSlotId": 0,
                                    "playerPoolEntry": {
                                        "player": {
                                            "id": 101,
                                            "fullName": "Historical Starter",
                                            "eligibleSlots": [0, 20],
                                            "injuryStatus": "ACTIVE",
                                            "proTeamId": 8,
                                            "stats": [],
                                        }
                                    },
                                }
                            ]
                        },
                    }
                }
            ]
        }

    def fetch_season(self, year, *, views=None):
        self.season_calls.append({"year": year, "views": tuple(views or ())})
        return {
            "settings": {
                "proTeams": [
                    {"id": 8, "proGamesByScoringPeriod": {"16": [{"id": 1}]}}
                ]
            }
        }


class ESPNPhaseCHistoricalLineupTests(unittest.TestCase):
    def test_server_helper_maps_multiweek_scoring_week_to_matchup_period(self):
        transport = FakeHistoricalTransport()
        settings = SimpleNamespace(matchup_periods={15: [15, 16]})

        with patch.object(espn.api, "get_transport", return_value=transport):
            boxes = espn._fetch_historical_lineup_boxes(7, 2026, 16, settings)

        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0].home_team.team_id, 1)
        self.assertFalse(boxes[0].home_lineup[0].on_bye_week)
        self.assertEqual(len(transport.league_calls), 1)
        call = transport.league_calls[0]
        self.assertEqual(call["scoring_period_id"], 16)
        self.assertEqual(
            call["fantasy_filter"],
            {"schedule": {"filterMatchupPeriodIds": {"value": [15]}}},
        )
        self.assertEqual(len(transport.season_calls), 1)
        self.assertEqual(transport.season_calls[0]["views"], ("proTeamSchedules_wl",))

    def test_production_server_has_no_remaining_wrapper_box_scores_call(self):
        source = Path(espn.__file__).read_text(encoding="utf-8")
        self.assertNotIn("league.box_scores(", source)


if __name__ == "__main__":
    unittest.main()
