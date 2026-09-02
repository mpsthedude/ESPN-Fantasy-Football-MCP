import pathlib
import unittest


class ESPNCommissionerFoundationParserTests(unittest.TestCase):
    def _payload(self):
        return {
            "scoringPeriodId": 19,
            "status": {"finalScoringPeriod": 18},
            "settings": {
                "name": "Commissioner League",
                "size": 2,
                "scheduleSettings": {
                    "matchupPeriodCount": 14,
                    "matchupPeriods": {str(i): [i] for i in range(1, 19)},
                    "playoffTeamCount": 4,
                    "playoffMatchupPeriodLength": 1,
                    "divisions": [
                        {"id": 2, "name": "West"},
                        {"id": 1, "name": "East"},
                    ],
                },
                "tradeSettings": {
                    "vetoVotesRequired": 4,
                    "deadlineDate": 1760000000000,
                },
                "draftSettings": {"keeperCount": 2},
                "acquisitionSettings": {
                    "isUsingAcquisitionBudget": True,
                    "acquisitionBudget": 100,
                },
                "rosterSettings": {
                    "lineupSlotCounts": {
                        "0": 1, "1": 0, "2": 2, "3": 0, "4": 2,
                        "5": 0, "6": 1, "7": 1, "8": 1, "9": 16,
                        "10": 1,
                    }
                },
                "scoringSettings": {},
            },
            "teams": [
                {
                    "id": 9,
                    "name": "Nine",
                    "roster": {"entries": [{
                        "lineupSlotId": 0,
                        "playerPoolEntry": {"player": {
                            "id": 9001,
                            "fullName": "Quarter Back",
                            "eligibleSlots": [0, 7, 20],
                            "injuryStatus": "ACTIVE",
                        }},
                    }]},
                },
                {
                    "id": 3,
                    "name": "Unknown",
                    "location": "Three",
                    "nickname": "Club",
                    "roster": {"entries": [{
                        "lineupSlotId": 20,
                        "playerPoolEntry": {"player": {
                            "id": 3001,
                            "fullName": "Wide Receiver",
                            "eligibleSlots": [4, 5, 6, 23],
                            "injuryStatus": "OUT",
                        }},
                    }]},
                },
            ],
        }

    def test_snapshot_preserves_governance_and_current_roster_contract(self):
        from espn_roster_read import build_commissioner_snapshot

        snapshot = build_commissioner_snapshot(self._payload(), 12345, 2026)
        self.assertEqual(12345, snapshot.league_id)
        self.assertEqual(2026, snapshot.year)
        self.assertEqual(18, snapshot.current_week)

        settings = snapshot.settings
        self.assertEqual("Commissioner League", settings.name)
        self.assertEqual(2, settings.team_count)
        self.assertEqual(14, settings.reg_season_count)
        self.assertEqual(4, settings.playoff_team_count)
        self.assertEqual(1, settings.playoff_matchup_period_length)
        self.assertEqual(4, settings.veto_votes_required)
        self.assertEqual(1760000000000, settings.trade_deadline)
        self.assertTrue(settings.faab)
        self.assertEqual(100, settings.acquisition_budget)
        self.assertEqual(2, settings.keeper_count)
        self.assertEqual({1: "East", 2: "West"}, settings.division_map)
        self.assertIn("1", settings.matchup_periods)
        self.assertEqual(1, settings.position_slot_counts["QB"])

        self.assertEqual([3, 9], [team.team_id for team in snapshot.teams])
        self.assertEqual("Three Club", snapshot.teams[0].team_name)
        player = snapshot.teams[0].roster[0]
        self.assertEqual(3001, player.playerId)
        self.assertEqual("Wide Receiver", player.name)
        self.assertEqual("BE", player.lineupSlot)
        self.assertIn("WR", player.eligibleSlots)
        self.assertEqual("OUT", player.injuryStatus)

    def test_pre_2018_current_week_preserves_uncapped_wrapper_behavior(self):
        from espn_roster_read import build_commissioner_snapshot

        payload = self._payload()
        snapshot = build_commissioner_snapshot(payload, 12345, 2017)
        self.assertEqual(19, snapshot.current_week)


class ESPNCommissionerFoundationSourceBoundaryTests(unittest.TestCase):
    def _function(self, source, name, next_marker):
        start = source.index(f"async def {name}(")
        end = source.index(next_marker, start)
        return source[start:end]

    def test_basic_commissioner_tools_use_project_owned_snapshot(self):
        source = pathlib.Path("espn_fantasy_server.py").read_text(encoding="utf-8")
        funcs = [
            self._function(source, "get_commissioner_context", "\n# --- COMMISSIONER READ/AUDIT - PHASE C2"),
            self._function(source, "commissioner_audit_lineups", "\n@mcp.tool()\nasync def commissioner_audit_rosters"),
            self._function(source, "commissioner_audit_rosters", "\n# --- COMMISSIONER READ/AUDIT - PHASE C3"),
        ]
        for func in funcs:
            self.assertIn("_fetch_commissioner_current_payload(", func)
            self.assertIn("build_commissioner_snapshot(", func)
            self.assertNotIn("api.get_league(", func)

    def test_current_commissioner_fetch_uses_narrow_project_owned_views(self):
        source = pathlib.Path("espn_fantasy_server.py").read_text(encoding="utf-8")
        start = source.index("def _fetch_commissioner_current_payload(")
        end = source.index("\ndef ", start + 4)
        helper = source[start:end]
        self.assertIn("COMMISSIONER_CURRENT_VIEWS", helper)
        self.assertIn("transport.fetch_league(", helper)


if __name__ == "__main__":
    unittest.main()
