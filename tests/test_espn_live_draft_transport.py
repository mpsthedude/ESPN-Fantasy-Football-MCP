import inspect
import unittest

import espn_fantasy_server as server
from espn_free_agent_read import build_free_agents


class LiveDraftTransportTests(unittest.TestCase):
    def test_live_draft_entry_points_do_not_call_wrapper_get_league(self):
        source = inspect.getsource(server)
        for name in ("get_draft_board", "prepare_draft_strategy"):
            start = source.index(f"async def {name}")
            tail = source[start:]
            next_tool = tail.find("\n    @mcp.tool()", 1)
            body = tail if next_tool == -1 else tail[:next_tool]
            self.assertNotIn("api.get_league(", body, name)
        helper_start = source.index("def _fetch_raw_draft_state")
        helper_tail = source[helper_start:]
        helper_end = helper_tail.index("\n    def ", 1)
        helper = helper_tail[:helper_end]
        self.assertIn("transport.fetch_league", helper)
        self.assertNotIn("espn_request", helper)

    def test_internal_free_agent_identity_is_opt_in(self):
        player_payload = {"players": [{"player": {
            "id": 99, "fullName": "Test Runner", "eligibleSlots": [2],
            "defaultPositionId": 2, "proTeamId": 1, "injured": False,
            "injuryStatus": "ACTIVE", "stats": []}}]}
        schedule_payload = {"settings": {"proTeams": [{
            "id": 1, "proGamesByScoringPeriod": {"1": [{"awayProTeamId": 1, "homeProTeamId": 2}]}}]}}
        public = build_free_agents(player_payload, schedule_payload, 2026, 1)
        internal = build_free_agents(player_payload, schedule_payload, 2026, 1, include_internal=True)
        self.assertNotIn("_player_id", public[0])
        self.assertEqual(internal[0]["_player_id"], 99)
        self.assertEqual(internal[0]["_injury_status"], "ACTIVE")


if __name__ == "__main__":
    unittest.main()
