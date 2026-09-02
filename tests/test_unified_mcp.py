import json
import os
import subprocess
import sys
import unittest


SPORTSGAMEODDS_TOOL_NAMES = {
    "get_sportsbook_usage",
    "find_sportsbook_team",
    "get_sportsbook_slate",
    "get_sportsbook_player_props",
    "compare_sportsbook_market",
    "find_sportsbook_market_disagreements",
    "find_sportsbook_player_prop_disagreements",
    "get_nfl_sportsbook_slate",
    "get_nfl_player_props",
    "get_fantasy_market_signal",
    "get_supported_sportsbook_leagues",
    "get_supported_sportsbooks",
}

MARKET_CONTEXT_TOOL_NAMES = {
    "get_player_prop_market_context",
}

ESPN_DISCOVERY_TOOL_NAMES = {
    "discover_my_espn_leagues",
    "sync_my_espn_leagues",
}


class UnifiedMCPTests(unittest.TestCase):
    def test_unified_server_preserves_existing_and_adds_provider_tools(self):
        # Run in a separate interpreter so importing the unified wrapper does
        # not mutate espn_fantasy_server.mcp inside the main unittest process.
        code = r'''
import asyncio, json
import espn_fantasy_server as espn
before = {tool.name for tool in asyncio.run(espn.mcp.list_tools())}
import fantasy_football_server as unified
after = {tool.name for tool in asyncio.run(unified.mcp.list_tools())}
print(json.dumps({
    "before_count": len(before),
    "after_count": len(after),
    "same_instance": unified.mcp is espn.mcp,
    "before": sorted(before),
    "after": sorted(after),
}))
'''
        env = os.environ.copy()
        env.pop("ESPN_S2", None)
        env.pop("ESPN_SWID", None)
        env.pop("SWID", None)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        before = set(payload["before"])
        after = set(payload["after"])

        self.assertEqual(payload["before_count"], 37)
        self.assertEqual(payload["after_count"], 52)
        self.assertTrue(payload["same_instance"])
        self.assertEqual(
            after - before,
            SPORTSGAMEODDS_TOOL_NAMES | MARKET_CONTEXT_TOOL_NAMES | ESPN_DISCOVERY_TOOL_NAMES,
        )
        self.assertTrue(before.issubset(after))

    def test_sportsbook_slate_schema_exposes_optional_cursor(self):
        code = r'''
import asyncio, json
import fantasy_football_server as unified
tool = next(t for t in asyncio.run(unified.mcp.list_tools()) if t.name == "get_sportsbook_slate")
print(json.dumps(tool.inputSchema))
'''
        env = os.environ.copy()
        env.pop("SPORTSGAMEODDS_API_KEY", None)
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
        )
        schema = json.loads(proc.stdout.strip().splitlines()[-1])
        properties = schema.get("properties", {})
        self.assertIn("cursor", properties)
        self.assertIsNone(properties["cursor"].get("default"))
        self.assertIn("team_id", properties)
        self.assertIsNone(properties["team_id"].get("default"))
        self.assertNotIn("team_id", schema.get("required", []))
        self.assertNotIn("cursor", schema.get("required", []))
        for field in ("starts_after", "starts_before"):
            self.assertIn(field, properties)
            self.assertIsNone(properties[field].get("default"))
            self.assertNotIn(field, schema.get("required", []))

    def test_find_sportsbook_team_schema_requires_name_and_league_only(self):
        code = r'''
import asyncio, json
import fantasy_football_server as unified
tool = next(t for t in asyncio.run(unified.mcp.list_tools()) if t.name == "find_sportsbook_team")
print(json.dumps(tool.inputSchema))
'''
        env = os.environ.copy()
        env.pop("SPORTSGAMEODDS_API_KEY", None)
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
        )
        schema = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(sorted(schema.get("required", [])), ["league", "team_name"])
        properties = schema.get("properties", {})
        self.assertIn("cursor", properties)
        self.assertIsNone(properties["cursor"].get("default"))
        self.assertEqual(properties["limit"].get("default"), 100)

    def test_sportsbook_market_comparison_schema_requires_exact_event_scope(self):
        code = r'''
import asyncio, json
import fantasy_football_server as unified
tool = next(t for t in asyncio.run(unified.mcp.list_tools()) if t.name == "compare_sportsbook_market")
print(json.dumps(tool.inputSchema))
'''
        env = os.environ.copy()
        env.pop("SPORTSGAMEODDS_API_KEY", None)
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
        )
        schema = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(sorted(schema.get("required", [])), ["event_id", "league", "market"])
        properties = schema.get("properties", {})
        for optional in ("bookmakers", "player_name", "team_id", "stat_id", "bet_type"):
            self.assertIn(optional, properties)
            self.assertNotIn(optional, schema.get("required", []))

    def test_sportsbook_market_disagreement_schema_requires_market_only(self):
        code = r'''
import asyncio, json
import fantasy_football_server as unified
tool = next(t for t in asyncio.run(unified.mcp.list_tools()) if t.name == "find_sportsbook_market_disagreements")
print(json.dumps(tool.inputSchema))
'''
        env = os.environ.copy()
        env.pop("SPORTSGAMEODDS_API_KEY", None)
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
        )
        schema = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(schema.get("required", []), ["market"])
        properties = schema.get("properties", {})
        for optional in (
            "league", "sport", "team_id", "bookmakers", "starts_after", "starts_before",
            "cursor", "limit", "top_n", "min_bookmakers",
        ):
            self.assertIn(optional, properties)
            self.assertNotIn(optional, schema.get("required", []))
        self.assertEqual(properties["limit"].get("default"), 20)
        self.assertEqual(properties["top_n"].get("default"), 10)
        self.assertEqual(properties["min_bookmakers"].get("default"), 2)

    def test_sportsbook_player_prop_disagreement_schema_requires_exact_scope(self):
        code = r'''
import asyncio, json
import fantasy_football_server as unified
tool = next(t for t in asyncio.run(unified.mcp.list_tools()) if t.name == "find_sportsbook_player_prop_disagreements")
print(json.dumps(tool.inputSchema))
'''
        env = os.environ.copy()
        env.pop("SPORTSGAMEODDS_API_KEY", None)
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
        )
        schema = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(
            sorted(schema.get("required", [])),
            ["event_id", "league", "player_name", "team_id"],
        )
        properties = schema.get("properties", {})
        for optional in ("stat_id", "bet_type", "bookmakers", "top_n", "min_bookmakers"):
            self.assertIn(optional, properties)
            self.assertNotIn(optional, schema.get("required", []))
        self.assertEqual(properties["top_n"].get("default"), 10)
        self.assertEqual(properties["min_bookmakers"].get("default"), 2)

    def test_player_prop_market_context_schema_requires_exact_sportsbook_scope(self):
        code = r'''
import asyncio, json
import fantasy_football_server as unified
tool = next(t for t in asyncio.run(unified.mcp.list_tools()) if t.name == "get_player_prop_market_context")
print(json.dumps(tool.inputSchema))
'''
        env = os.environ.copy()
        env.pop("SPORTSGAMEODDS_API_KEY", None)
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
        )
        schema = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(
            sorted(schema.get("required", [])),
            ["event_id", "league", "player_name", "team_id"],
        )
        properties = schema.get("properties", {})
        for optional in (
            "espn_league_id", "espn_year", "scoring", "stat_id", "bet_type",
            "bookmakers", "top_n", "min_bookmakers",
        ):
            self.assertIn(optional, properties)
        self.assertEqual(properties["scoring"].get("default"), "PPR")
        self.assertEqual(properties["top_n"].get("default"), 5)
        self.assertEqual(properties["min_bookmakers"].get("default"), 2)

    def test_sportsbook_player_props_schema_exposes_optional_event_id(self):
        code = r'''
import asyncio, json
import fantasy_football_server as unified
tool = next(t for t in asyncio.run(unified.mcp.list_tools()) if t.name == "get_sportsbook_player_props")
print(json.dumps(tool.inputSchema))
'''
        env = os.environ.copy()
        env.pop("SPORTSGAMEODDS_API_KEY", None)
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
        )
        schema = json.loads(proc.stdout.strip().splitlines()[-1])
        properties = schema.get("properties", {})
        self.assertIn("event_id", properties)
        self.assertIsNone(properties["event_id"].get("default"))
        self.assertNotIn("event_id", schema.get("required", []))
        self.assertIn("team_id", schema.get("required", []))

    def test_unified_server_bootstraps_espn_credentials_from_quick_desktop_environment(self):
        # Regression: list_my_leagues reads api.credentials before making its
        # first get_league() call. The old lazy loader therefore ran too late
        # for registry/team resolution. Unified startup must preload the shared
        # default session when the MCP host supplies ESPN_S2 + SWID.
        code = r'''
import json
import fantasy_football_server as unified
creds = unified.api.credentials.get(unified.SESSION_ID, {})
print(json.dumps({
    "has_s2": bool(creds.get("espn_s2")),
    "has_swid": bool(creds.get("swid")),
    "swid_alias_mirrored": bool(__import__("os").environ.get("ESPN_SWID")),
}))
'''
        env = os.environ.copy()
        env["ESPN_S2"] = "SYNTHETIC_TEST_S2"
        env.pop("ESPN_SWID", None)
        env["SWID"] = "{SYNTHETIC-TEST-SWID}"
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["has_s2"])
        self.assertTrue(payload["has_swid"])
        self.assertTrue(payload["swid_alias_mirrored"])
        self.assertNotIn("SYNTHETIC_TEST_S2", proc.stdout)
        self.assertNotIn("SYNTHETIC-TEST-SWID", proc.stdout)

    def test_unified_server_bootstraps_project_named_espn_swid(self):
        code = r'''
import json
import fantasy_football_server as unified
creds = unified.api.credentials.get(unified.SESSION_ID, {})
print(json.dumps({
    "has_s2": bool(creds.get("espn_s2")),
    "has_swid": bool(creds.get("swid")),
}))
'''
        env = os.environ.copy()
        env["ESPN_S2"] = "SYNTHETIC_TEST_S2"
        env["ESPN_SWID"] = "{SYNTHETIC-TEST-SWID}"
        env.pop("SWID", None)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["has_s2"])
        self.assertTrue(payload["has_swid"])

    def test_authenticate_tool_schema_allows_environment_only_call(self):
        # Quick Desktop reasons from the MCP JSON schema. The unified tool must
        # therefore advertise zero required fields when server-side auth is the
        # normal path, while still exposing optional explicit override fields.
        code = r'''
import asyncio, json
import fantasy_football_server as unified
tools = asyncio.run(unified.mcp.list_tools())
auth = next(tool for tool in tools if tool.name == "authenticate")
schema = auth.inputSchema
print(json.dumps({
    "required": schema.get("required", []),
    "properties": sorted(schema.get("properties", {}).keys()),
}))
'''
        env = os.environ.copy()
        env.pop("ESPN_S2", None)
        env.pop("ESPN_SWID", None)
        env.pop("SWID", None)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["required"], [])
        self.assertEqual(payload["properties"], ["espn_s2", "swid"])

    def test_authenticate_without_arguments_reports_bootstrapped_environment_auth(self):
        code = r'''
import asyncio, json
import fantasy_football_server as unified
message = asyncio.run(unified.authenticate())
print(json.dumps({
    "active": "already active" in message.lower(),
    "manual_not_required": "no manual credential call is required" in message.lower(),
}))
'''
        synthetic_s2 = "SYNTHETIC_TEST_S2_NEVER_PRINT"
        synthetic_swid = "{SYNTHETIC-TEST-SWID-NEVER-PRINT}"
        env = os.environ.copy()
        env["ESPN_S2"] = synthetic_s2
        env.pop("ESPN_SWID", None)
        env["SWID"] = synthetic_swid
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["active"])
        self.assertTrue(payload["manual_not_required"])
        self.assertNotIn(synthetic_s2, proc.stdout)
        self.assertNotIn(synthetic_s2, proc.stderr)
        self.assertNotIn(synthetic_swid, proc.stdout)
        self.assertNotIn(synthetic_swid, proc.stderr)

    def test_authenticate_rejects_partial_explicit_override(self):
        code = r'''
import asyncio, json
import fantasy_football_server as unified
message = asyncio.run(unified.authenticate(espn_s2="SYNTHETIC_ONLY_ONE"))
print(json.dumps({"partial_rejected": "provide both" in message.lower()}))
'''
        env = os.environ.copy()
        env.pop("ESPN_S2", None)
        env.pop("ESPN_SWID", None)
        env.pop("SWID", None)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["partial_rejected"])
        self.assertNotIn("SYNTHETIC_ONLY_ONE", proc.stdout)
        self.assertNotIn("SYNTHETIC_ONLY_ONE", proc.stderr)

    def test_fantasypros_request_uses_quick_desktop_process_environment_without_leaking_key(self):
        # Quick Desktop injects MCP secrets into the child process environment.
        # Prove the real FantasyPros request boundary resolves that environment
        # value, places it in the outbound x-api-key header, and never prints or
        # returns the secret. The network, quota ledger, and pacing are mocked;
        # no real API call or credential is used.
        code = r'''
import json
import os
import fantasypros_client as fp

# Force the portable process-environment path even on Windows machines that
# may have a real User-scope FANTASYPROS_API_KEY configured in the registry.
fp._read_windows_user_env_registry = lambda name: None
fp._check_quota_guard = lambda allow_override: None
fp._pace_before_request = lambda: None
fp._increment_usage = lambda: None

captured = {"header_matched": False}

class FakeResponse:
    status_code = 200
    headers = {}
    def raise_for_status(self):
        return None
    def json(self):
        return {"ok": True}

def fake_get(url, headers=None, params=None, timeout=None):
    captured["header_matched"] = (
        isinstance(headers, dict)
        and headers.get("x-api-key") == os.environ.get("FANTASYPROS_API_KEY")
    )
    return FakeResponse()

fp.requests.get = fake_get
key, source = fp._resolve_fantasypros_api_key_with_source()
result = fp._request("/nfl/players")
print(json.dumps({
    "source": source,
    "resolved": bool(key),
    "header_matched": captured["header_matched"],
    "request_ok": result.get("ok") is True,
}))
'''
        synthetic_secret = "SYNTHETIC_FP_KEY_DO_NOT_PRINT_847291"
        env = os.environ.copy()
        env["FANTASYPROS_API_KEY"] = synthetic_secret
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["source"], "process_environment")
        self.assertTrue(payload["resolved"])
        self.assertTrue(payload["header_matched"])
        self.assertTrue(payload["request_ok"])
        self.assertNotIn(synthetic_secret, proc.stdout)
        self.assertNotIn(synthetic_secret, proc.stderr)

    def test_env_placeholder_cannot_overwrite_bootstrapped_session(self):
        code = r'''
import json
import fantasy_football_server as unified
before = dict(unified.api.credentials.get(unified.SESSION_ID, {}))
rejected = False
try:
    unified.api.store_credentials(unified.SESSION_ID, "ENV", "ENV")
except ValueError:
    rejected = True
after = dict(unified.api.credentials.get(unified.SESSION_ID, {}))
print(json.dumps({
    "rejected": rejected,
    "preserved": before == after,
    "still_authenticated": bool(after.get("espn_s2")) and bool(after.get("swid")),
}))
'''
        env = os.environ.copy()
        env["ESPN_S2"] = "SYNTHETIC_TEST_S2"
        env.pop("ESPN_SWID", None)
        env["SWID"] = "{SYNTHETIC-TEST-SWID}"
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["rejected"])
        self.assertTrue(payload["preserved"])
        self.assertTrue(payload["still_authenticated"])


if __name__ == "__main__":
    unittest.main()
