import datetime
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import espn_activity_read as ar
import espn_fantasy_server as server


class ActivityReadTests(unittest.TestCase):
    def _league(self):
        p1 = SimpleNamespace(playerId=101, name="Roster Player")
        t1 = SimpleNamespace(team_id=1, team_name="One", roster=[p1])
        t2 = SimpleNamespace(team_id=2, team_name="Two", roster=[])
        return SimpleNamespace(league_id=77, year=2026, teams=[t1, t2])

    def test_filter_matches_mixed_recent_activity_contract(self):
        filt = ar.build_activity_filter(25, 50)
        topics = filt["topics"]
        self.assertEqual(topics["filterType"]["value"], ["ACTIVITY_TRANSACTIONS"])
        self.assertEqual(topics["limit"], 25)
        self.assertEqual(topics["offset"], 50)
        self.assertEqual(topics["limitPerMessageSet"]["value"], 25)
        self.assertEqual(topics["filterIncludeMessageTypeIds"]["value"], [178, 180, 179, 239, 181, 244])

    def test_build_events_preserves_order_trade_expansion_and_waiver_bid(self):
        ts = 1_750_000_000_000
        payload = {"topics": [
            {"date": ts, "messages": [
                {"messageTypeId": 180, "to": 1, "targetId": 202, "from": 17},
                {"messageTypeId": 179, "to": 1, "targetId": 101},
            ]},
            {"date": ts - 1000, "messages": [
                {"messageTypeId": 244, "from": 1, "to": 2, "targetId": 303},
            ]},
        ]}
        events = ar.build_activity_events(payload, self._league(), {202: "Waiver Player", 303: "Trade Player"})
        self.assertEqual([e["event_type"] for e in events], ["waiver", "trade"])
        waiver = events[0]
        self.assertTrue(waiver["paired_add_drop"])
        self.assertEqual([a["action_type"] for a in waiver["actions"]], ["waiver_add", "drop"])
        self.assertEqual(waiver["actions"][0]["bid_amount"], 17)
        self.assertIsNone(waiver["actions"][1]["bid_amount"])
        self.assertEqual(waiver["actions"][1]["player_name"], "Roster Player")
        trade = events[1]
        self.assertEqual([a["source_action"] for a in trade["actions"]], ["TRADE_SENT", "TRADE_RECEIVED"])
        self.assertEqual([a["team_id"] for a in trade["actions"]], [1, 2])
        self.assertEqual(trade["actions"][0]["player_id"], 303)
        self.assertEqual(trade["actions"][0]["player_name"], "Trade Player")
        self.assertEqual(
            waiver["timestamp_utc"],
            datetime.datetime.fromtimestamp(ts / 1000, tz=datetime.timezone.utc).isoformat(),
        )

    def test_unresolved_historical_name_keeps_factual_target_id_without_hidden_lookup(self):
        payload = {"topics": [{"date": 1_750_000_000_000, "messages": [
            {"messageTypeId": 178, "to": 1, "targetId": 999},
        ]}]}
        event = ar.build_activity_events(payload, self._league(), {})[0]
        action = event["actions"][0]
        self.assertEqual(action["player_id"], 999)
        self.assertIsNone(action["player_name"])

    def test_activity_scanner_and_migrated_tools_have_no_recent_activity_or_wrapper_fetch(self):
        scanner_src = inspect.getsource(server._commissioner_fetch_activity_events)
        self.assertNotIn("recent_activity(", scanner_src)
        for fn in (server.commissioner_audit_transactions, server.get_commissioner_brief):
            src = inspect.getsource(fn)
            self.assertNotIn("api.get_league(", src)


if __name__ == "__main__":
    unittest.main()
