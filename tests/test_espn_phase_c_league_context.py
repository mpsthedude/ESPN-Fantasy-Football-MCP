import pathlib
import unittest

from espn_league_read import resolve_my_team_from_payload


class ESPNLeagueContextOwnershipTests(unittest.TestCase):
    def _payload(self):
        return {
            "settings": {"name": "Test League"},
            "members": [
                {"id": "{OWNER-A}", "displayName": "Owner A"},
                {"id": "{OWNER-B}", "displayName": "Owner B"},
            ],
            "teams": [
                {"id": 1, "name": "Alpha", "owners": ["{OWNER-A}"]},
                {"id": 2, "location": "Beta", "nickname": "Club", "owners": ["{OWNER-B}"]},
            ],
        }

    def test_single_owner_match_is_resolved_without_exposing_owner_id(self):
        result = resolve_my_team_from_payload(self._payload(), "owner-a")
        self.assertEqual("resolved", result["status"])
        self.assertEqual(1, result["team_id"])
        self.assertEqual("Alpha", result["team_name"])
        self.assertEqual("owner_swid_match", result["resolution_method"])
        self.assertEqual([], result["candidates"])
        self.assertNotIn("owner-a", str(result).lower())

    def test_missing_credential_preserves_legacy_unresolved_contract(self):
        result = resolve_my_team_from_payload(self._payload(), None)
        self.assertEqual({
            "status": "team_not_resolved",
            "team_id": None,
            "team_name": None,
            "resolution_method": "no_credential_available",
            "candidates": [],
        }, result)

    def test_ambiguous_owner_match_is_reported_not_guessed(self):
        payload = self._payload()
        payload["teams"][1]["owners"] = ["{OWNER-A}"]
        result = resolve_my_team_from_payload(payload, "{owner-a}")
        self.assertEqual("ambiguous_team_ownership", result["status"])
        self.assertIsNone(result["team_id"])
        self.assertEqual([
            {"team_id": 1, "team_name": "Alpha"},
            {"team_id": 2, "team_name": "Beta Club"},
        ], result["candidates"])


class ESPNLeagueContextSourceBoundaryTests(unittest.TestCase):
    def test_registry_context_helper_uses_project_owned_espn_read(self):
        source = pathlib.Path("espn_fantasy_server.py").read_text(encoding="utf-8")
        start = source.index("def _league_context_for_entry(")
        end = source.index("\n@mcp.tool()\nasync def list_my_leagues", start)
        helper = source[start:end]

        self.assertIn("_fetch_core_league_payload(", helper)
        self.assertIn("build_league_settings(", helper)
        self.assertIn("resolve_my_team_from_payload(", helper)
        self.assertNotIn("api.get_league(", helper)
        self.assertNotIn("league.settings", helper)
        self.assertNotIn("_resolve_my_team(league", helper)


if __name__ == "__main__":
    unittest.main()
