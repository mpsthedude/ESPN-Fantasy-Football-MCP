import unittest

from espn_snapshot_read import (
    SNAPSHOT_VIEWS,
    ESPNSnapshotPayloadError,
    build_league_snapshot_base,
)


class ESPNSnapshotReadTests(unittest.TestCase):
    def _payload(self):
        return {
            "seasonId": 2026,
            "scoringPeriodId": 7,
            "status": {"latestScoringPeriod": 7, "finalScoringPeriod": 17},
            "settings": {
                "name": "Snapshot League",
                "size": 2,
                "scheduleSettings": {"playoffTeamCount": 2},
                "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 2}},
                "scoringSettings": {
                    "scoringType": "H2H_POINTS",
                    "scoringItems": [{"statId": 53, "points": 1.0}],
                },
            },
            "draftDetail": {
                "drafted": True,
                "picks": [{"roundId": 1, "roundPickNumber": 1, "playerId": 101, "teamId": 1}],
            },
            # Deliberately reverse team-id order to verify wrapper-compatible
            # roster ordering while standings remain rank ordered.
            "teams": [
                {
                    "id": 2,
                    "name": "Second Team",
                    "rankCalculatedFinal": 0,
                    "playoffSeed": 1,
                    "record": {"overall": {"wins": 6, "losses": 1, "pointsFor": 700.25, "pointsAgainst": 600.129}},
                    "roster": {"entries": [{
                        "lineupSlotId": 0,
                        "playerPoolEntry": {"player": {
                            "fullName": "Quarterback Two",
                            "eligibleSlots": [0],
                            "proTeamId": 2,
                            "stats": [{
                                "seasonId": 2026,
                                "statSplitTypeId": 0,
                                "scoringPeriodId": 0,
                                "statSourceId": 1,
                                "appliedTotal": 301.257,
                                "appliedAverage": 17.72,
                                "stats": {},
                                "appliedStats": {},
                            }],
                        }},
                    }]},
                },
                {
                    "id": 1,
                    "location": "First",
                    "nickname": "Team",
                    "rankCalculatedFinal": 2,
                    "playoffSeed": 2,
                    "record": {"overall": {"wins": 4, "losses": 3, "pointsFor": 650.0, "pointsAgainst": 625.555}},
                    "roster": {"entries": [{
                        "lineupSlotId": 2,
                        "playerPoolEntry": {"player": {
                            "fullName": "Running Back One",
                            "eligibleSlots": [2, 23],
                            "proTeamId": 1,
                            "stats": [{
                                "seasonId": 2026,
                                "statSplitTypeId": 0,
                                "scoringPeriodId": 0,
                                "statSourceId": 1,
                                "appliedTotal": 250.444,
                                "appliedAverage": 14.73,
                                "stats": {},
                                "appliedStats": {},
                            }],
                        }},
                    }]},
                },
            ],
            "schedule": [],
        }

    def test_snapshot_views_cover_existing_direct_contracts(self):
        self.assertEqual(
            SNAPSHOT_VIEWS,
            ("mTeam", "mMatchup", "mSettings", "mStandings", "mRoster", "mDraftDetail"),
        )

    def test_build_snapshot_preserves_legacy_shape_and_ordering(self):
        result = build_league_snapshot_base(self._payload(), 55, 2026)

        self.assertEqual(result["league_id"], 55)
        self.assertEqual(result["year"], 2026)
        self.assertEqual(result["league_name"], "Snapshot League")
        self.assertEqual(result["current_week"], 7)
        self.assertEqual(result["scoring_type"], "H2H_POINTS")
        self.assertTrue(result["draft_completed"])

        # League.standings(): seed/final rank order.
        self.assertEqual([row["team_id"] for row in result["standings"]], [2, 1])
        self.assertEqual(result["standings"][0]["rank"], 1)
        self.assertEqual(result["standings"][0]["points_against"], 600.13)
        self.assertEqual(result["standings"][1]["team_name"], "First Team")

        # League.teams: team-id order.
        self.assertEqual([row["team_id"] for row in result["rosters"]], [1, 2])
        self.assertEqual(result["rosters"][0]["roster"][0]["name"], "Running Back One")
        self.assertEqual(result["rosters"][0]["roster"][0]["projected_points"], 250.44)

    def test_drafted_flag_without_picks_matches_bool_legacy_draft_list(self):
        payload = self._payload()
        payload["draftDetail"] = {"drafted": True, "picks": []}
        result = build_league_snapshot_base(payload, 55, 2026)
        self.assertFalse(result["draft_completed"])

    def test_invalid_draft_collection_fails_closed(self):
        payload = self._payload()
        payload["draftDetail"] = {"drafted": True, "picks": {}}
        with self.assertRaises(ESPNSnapshotPayloadError):
            build_league_snapshot_base(payload, 55, 2026)


if __name__ == "__main__":
    unittest.main()
