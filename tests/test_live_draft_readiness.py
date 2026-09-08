import asyncio
import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import espn_fantasy_server as server
from espn_draft_read import ESPNDraftPayloadError, build_live_player_pool, validate_live_snapshot


class LiveDraftReadinessTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        home = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, {"FANTASY_FOOTBALL_MCP_HOME": home}))
        Path(home, "league_registry.json").write_text(json.dumps({
            "version": 1, "default_league": "test", "leagues": {
                "test": {"league_id": 123456, "display_name": "Test", "enabled": True}}}))
        self.stack.enter_context(patch.dict(server.api.credentials, {server.SESSION_ID: {"swid": "{TEST}"}}))
        self.raw = {
            "seasonId": 2026, "scoringPeriodId": 1,
            "members": [{"id": "{TEST}"}, {"id": "{OTHER}"}],
            "settings": {"name": "Test", "size": 2, "scheduleSettings": {},
                         "draftSettings": {"type": "SNAKE", "keeperCount": 0},
                         "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 1, "4": 1, "6": 1, "20": 4}},
                         "scoringSettings": {"scoringItems": [{"statId": 53, "points": 1.0}]}},
            "teams": [{"id": 1, "name": "Opponent", "owners": ["{OTHER}"], "roster": {"entries": []}},
                      {"id": 14, "name": "My Test Team", "owners": ["{TEST}"], "roster": {"entries": []}}],
            "draftDetail": {"drafted": False, "inProgress": True, "picks": [
                {"overallPickNumber": i, "roundId": (i + 1) // 2, "roundPickNumber": 1 if i % 2 else 2,
                 "teamId": 1 if i % 2 else 14, "playerId": -1, "reservedForKeeper": False}
                for i in range(1, 17)]},
        }
        self.final = copy.deepcopy(self.raw)
        self.pool = {"players": []}
        self.rankings = {}
        self.projections = {}
        player_rows = []
        for pos, slot in [("QB", 0), ("RB", 2), ("WR", 4), ("TE", 6)]:
            ranks, projections = [], []
            for n in range(1, 9):
                name = f"{pos} Player {n}"
                norm = server.fp_client.normalize_player_name(name)
                pid = slot * 100 + n
                self.pool["players"].append({"status": "ONTEAM", "onTeamId": 1, "player": {
                    "id": pid, "fullName": name, "eligibleSlots": [slot], "proTeamId": 1,
                    "injuryStatus": "ACTIVE", "stats": [{"seasonId": 2026, "scoringPeriodId": 0,
                    "statSplitTypeId": 0, "statSourceId": 1, "appliedTotal": 300 - n * 10}]}})
                ranks.append({"fp_player_id": pid, "name": name, "_norm_name": norm,
                              "rank_ecr": n, "pos_rank": n, "tier": (n + 2) // 3})
                projections.append({"_norm_name": norm, "projected_points": 300 - n * 10})
                player_rows.append({"_norm_name": norm, "rank_adp_ppr": n * 4, "rank_adp": n * 4})
            self.rankings[pos] = {"fetched_at": "2026-09-08T12:00:00Z", "players": ranks}
            self.projections[pos] = {"fetched_at": "2026-09-08T12:00:00Z", "players": projections}
        self.rank_mock = self.stack.enter_context(patch.object(server.fp_client, "get_rankings_cache", side_effect=lambda p, s: self.rankings[p]))
        self.stack.enter_context(patch.object(server.fp_client, "get_projections_cache", side_effect=lambda p, s, week=0: self.projections[p]))
        self.stack.enter_context(patch.object(server.fp_client, "get_players_cache", return_value={"players": player_rows}))
        self.stack.enter_context(patch.object(server.fp_client, "get_injuries_cache", return_value={"injuries": []}))
        self.stack.enter_context(patch.object(server.fp_client, "get_cache_freshness_report", return_value={}))
        self.state_reads = 0
        self.pool_error = False
        self.transport = Mock()
        self.transport.fetch_league.side_effect = self.fetch
        self.stack.enter_context(patch.object(server.api, "get_transport", return_value=self.transport))

    def fetch(self, league_id, year, *, views, **kwargs):
        if "kona_player_info" in views:
            self.assertNotIn("filterStatus", kwargs["fantasy_filter"]["players"])
            self.assertEqual(kwargs["fantasy_filter"]["players"]["sortDraftRanks"]["value"], "PPR")
            if self.pool_error:
                raise RuntimeError("pool failure")
            return copy.deepcopy(self.pool)
        self.assertEqual(tuple(views), server._DRAFT_STATE_VIEWS)
        self.state_reads += 1
        return copy.deepcopy(self.raw if self.state_reads == 1 else self.final)

    def brief(self):
        return asyncio.run(server.get_live_draft_brief(league_id=123456, year=2026))

    def test_real_snapshot_team_resolution_and_ppr_recommendation(self):
        result = self.brief()
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["scoring_bucket"], "PPR")
        self.assertIsNotNone(result["recommendation"])
        self.assertEqual(result["headline"]["decision_pick"], 2)
        self.assertTrue(all(call.args[1] == "PPR" for call in self.rank_mock.call_args_list))

    def test_factual_board_uses_ppr_and_full_draft_pool(self):
        result = asyncio.run(server.get_draft_board(league_id=123456, year=2026))
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["scoring_bucket"], "PPR")
        self.assertEqual(result["my_team"]["team_id"], 14)
        self.assertEqual(sum(result["available"]["by_position_count"].values()), 32)

    def test_player_taken_during_call_is_excluded_even_before_roster_updates(self):
        original = self.brief()["recommendation"]["player_id"]
        self.state_reads = 0
        self.final["draftDetail"]["picks"][0]["playerId"] = original
        result = self.brief()
        self.assertTrue(result["board_advanced_during_call"])
        self.assertTrue(result["headline"]["user_on_clock"])
        self.assertEqual(result["completed_pick_count"], 1)
        self.assertNotEqual(result["recommendation"]["player_id"], original)
        self.assertNotIn(original, [p["player_id"] for p in result["alternatives"]])

    def test_draft_completion_during_call_returns_no_recommendation(self):
        self.final["draftDetail"].update(drafted=True, inProgress=False)
        result = self.brief()
        self.assertEqual(result["headline"]["draft_status"], "complete")
        self.assertIsNone(result["recommendation"])

    def test_empty_final_snapshot_fails_closed(self):
        self.final["draftDetail"]["picks"] = []
        self.assertEqual(self.brief()["error"], "draft_revalidation_failed")

    def test_partial_final_team_snapshot_fails_closed(self):
        self.final["teams"] = self.final["teams"][:1]
        self.assertEqual(self.brief()["error"], "draft_revalidation_failed")

    def test_player_pool_failure_is_explicit(self):
        self.pool_error = True
        self.assertEqual(self.brief()["error"], "draft_player_pool_unavailable")
        self.state_reads = 0
        result = asyncio.run(server.analyze_draft_pick(league_id=123456, year=2026))
        self.assertEqual(result["error"], "draft_player_pool_unavailable")

    def test_comparison_requires_retry_when_board_advances(self):
        self.final["draftDetail"]["picks"][0]["playerId"] = 1
        result = asyncio.run(server.analyze_draft_pick(league_id=123456, year=2026))
        self.assertEqual(result["error"], "draft_board_changed_retry")

    def test_pre_draft_keeper_preview_is_low_confidence(self):
        self.raw["draftDetail"]["inProgress"] = False
        self.raw["settings"]["draftSettings"]["keeperCount"] = 1
        self.raw["draftDetail"]["picks"][0]["reservedForKeeper"] = True
        self.final = copy.deepcopy(self.raw)
        result = self.brief()
        self.assertEqual(result["availability_status"], "provisional_keeper_assignment")
        self.assertEqual(result["recommendation"]["confidence"], "low")
        self.assertFalse(result["headline"]["user_on_clock"])
        self.assertEqual(result["headline"]["picks_until_decision"], 0)

    def test_keeper_assignment_during_call_requires_retry(self):
        self.raw["draftDetail"]["picks"][0]["reservedForKeeper"] = True
        self.final = copy.deepcopy(self.raw)
        self.final["draftDetail"]["picks"][0]["playerId"] = 1
        self.assertEqual(self.brief()["error"], "keeper_state_changed_retry")

    def test_player_pool_preserves_season_projection_and_carried_roster_players(self):
        players = build_live_player_pool(self.pool, 2026)
        self.assertEqual(len(players), 32)
        self.assertEqual(server._adp_espn_season_projection(players[0]), 290)

    def test_replacement_level_accounts_for_all_teams(self):
        universes = {p: [{"_norm_name": f"{p}{n}", "projection": 400 - n} for n in range(100)]
                     for p in ["QB", "RB", "WR", "TE"]}
        result = server._ds_starter_flex_allocation({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "RB/WR/TE": 1}, universes, 14)
        self.assertEqual(result["dedicated_demand"]["RB"], 2)
        self.assertEqual(result["replacement_index"]["QB"], 14)
        self.assertEqual(sum(result["replacement_index"].values()), 98)

    def test_duplicate_slots_rejected(self):
        self.raw["draftDetail"]["picks"][1]["overallPickNumber"] = 1
        with self.assertRaises(ESPNDraftPayloadError):
            validate_live_snapshot(self.raw)


if __name__ == "__main__":
    unittest.main()
