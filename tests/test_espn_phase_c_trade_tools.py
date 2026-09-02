import ast
import asyncio
import inspect
import textwrap
import unittest
from unittest.mock import patch

import espn_fantasy_server as espn
from espn_adapter import build_espn_league_snapshot_from_payload
from espn_league_read import build_league_settings


class ESPNPhaseCTradeToolTests(unittest.TestCase):
    @staticmethod
    def _player_entry(name, eligible_slots, pro_team_id, actual, projected,
                      lineup_slot_id, injury_status):
        return {
            "lineupSlotId": lineup_slot_id,
            "playerPoolEntry": {
                "player": {
                    "fullName": name,
                    "eligibleSlots": eligible_slots,
                    "proTeamId": pro_team_id,
                    "injuryStatus": injury_status,
                    "stats": [
                        {
                            "seasonId": 2026,
                            "statSplitTypeId": 0,
                            "scoringPeriodId": 0,
                            "statSourceId": 0,
                            "appliedTotal": actual,
                            "appliedAverage": actual / 3,
                            "stats": {},
                            "appliedStats": {},
                        },
                        {
                            "seasonId": 2026,
                            "statSplitTypeId": 0,
                            "scoringPeriodId": 0,
                            "statSourceId": 1,
                            "appliedTotal": projected,
                            "appliedAverage": projected / 17,
                            "stats": {},
                            "appliedStats": {},
                        },
                    ],
                }
            },
        }

    @classmethod
    def _payload(cls):
        return {
            "seasonId": 2026,
            "scoringPeriodId": 4,
            "status": {
                "latestScoringPeriod": 4,
                "finalScoringPeriod": 17,
                "currentMatchupPeriod": 4,
            },
            "settings": {
                "name": "Trade Test League",
                "size": 2,
                "scheduleSettings": {},
                "rosterSettings": {
                    "lineupSlotCounts": {"0": 1, "2": 1, "4": 1, "20": 2}
                },
                "scoringSettings": {
                    "scoringType": "H2H_POINTS",
                    "scoringItems": [{"statId": 53, "points": 1.0}],
                },
            },
            "teams": [
                {
                    "id": 7,
                    "name": "Seven Team",
                    "roster": {"entries": [
                        cls._player_entry(
                            "Seven Receiver", [4, 23], 21, 40.0, 210.5,
                            4, "QUESTIONABLE"
                        )
                    ]},
                },
                {
                    "id": 1,
                    "name": "One Team",
                    "roster": {"entries": [
                        cls._player_entry(
                            "One Runner", [2, 23], 1, 55.0, 225.25,
                            20, "ACTIVE"
                        )
                    ]},
                },
            ],
            "schedule": [],
            "draftDetail": {"drafted": True, "picks": [{"roundId": 1}]},
        }

    def _snapshot(self):
        payload = self._payload()
        settings = build_league_settings(payload, 55, 2026)
        return build_espn_league_snapshot_from_payload(
            payload,
            55,
            2026,
            settings["roster_slot_counts"],
            "PPR",
        )

    def test_domain_snapshot_preserves_trade_roster_state(self):
        snapshot = self._snapshot()
        one = next(t for t in snapshot.teams if t.team_id == 1)
        seven = next(t for t in snapshot.teams if t.team_id == 7)

        self.assertEqual(one.roster[0].lineup_slot, "BE")
        self.assertEqual(one.roster[0].injury_status, "ACTIVE")
        self.assertEqual(seven.roster[0].lineup_slot, "WR")
        self.assertEqual(seven.roster[0].injury_status, "QUESTIONABLE")

        resolved = espn._resolve_trade_players(
            snapshot, 1, ["One Runner"], ["Seven Receiver"]
        )
        self.assertNotIn("error", resolved)
        self.assertEqual(resolved["target_team"].team_name, "One Team")
        self.assertEqual(resolved["partner_team_id"], 7)
        self.assertEqual(resolved["target_roster_before"][0]["lineup_slot"], "BE")
        self.assertEqual(resolved["target_roster_before"][0]["espn_injury_status"], "ACTIVE")
        self.assertEqual(resolved["players_in_resolved"][0]["espn_injury_status"], "QUESTIONABLE")
        self.assertEqual(resolved["players_in_resolved"][0]["projected_points"], 210.5)

    def test_evaluate_trade_uses_direct_snapshot_before_cache_gate(self):
        payload = self._payload()
        with patch.object(
            espn, "_fetch_snapshot_payload", return_value=payload
        ) as fetch_snapshot, patch.object(
            espn, "_check_required_fp_caches", return_value=["synthetic missing cache"]
        ):
            result = asyncio.run(
                espn.evaluate_trade(55, 1, ["One Runner"], ["Seven Receiver"], year=2026)
            )

        fetch_snapshot.assert_called_once_with(55, 2026)
        self.assertEqual(result["error"], "cache_incomplete")
        self.assertEqual(result["scoring_bucket_detected"], "PPR")

    def test_find_trade_targets_uses_direct_snapshot_before_cache_gate(self):
        payload = self._payload()
        with patch.object(
            espn, "_fetch_snapshot_payload", return_value=payload
        ) as fetch_snapshot, patch.object(
            espn, "_check_required_fp_caches", return_value=["synthetic missing cache"]
        ):
            result = asyncio.run(espn.find_trade_targets(55, 1, year=2026))

        fetch_snapshot.assert_called_once_with(55, 2026)
        self.assertEqual(result["error"], "cache_incomplete")
        self.assertEqual(result["scoring_bucket_detected"], "PPR")

    def test_trade_tool_sources_have_no_wrapper_league_call(self):
        for func in (espn.evaluate_trade, espn.find_trade_targets):
            source = textwrap.dedent(inspect.getsource(inspect.unwrap(func)))
            tree = ast.parse(source)
            wrapper_calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get_league"
            ]
            self.assertEqual(wrapper_calls, [], func.__name__)


if __name__ == "__main__":
    unittest.main()
