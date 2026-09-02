import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import espn_league_discovery as discovery


class CandidateExtractionTests(unittest.TestCase):
    def test_extracts_direct_and_encoded_candidate_league_ids(self):
        payload = {
            "preferences": [
                {"leagueId": 123456, "game": "ffl"},
                {"id": "ffl~654321"},
                {"nested": {"league_id": "777777"}},
                {"leagueId": 999999, "game": "fba"},
            ]
        }
        # Shape-drifting direct leagueId fields are accepted as candidates;
        # _verify_league is the authoritative FFL/access filter afterward.
        self.assertEqual(
            discovery._candidate_ids_from_payload(payload),
            {123456, 654321, 777777, 999999},
        )

    def test_ignores_invalid_ids(self):
        payload = {"leagueId": "not-a-number", "value": "football:abc"}
        self.assertEqual(discovery._candidate_ids_from_payload(payload), set())


class RegistryMergeTests(unittest.TestCase):
    def test_preserves_existing_alias_and_adds_new_league(self):
        existing = {
            "version": 1,
            "default_league": "the_league",
            "leagues": {
                "the_league": {
                    "league_id": 1907081848,
                    "display_name": "The League",
                    "enabled": True,
                }
            },
        }
        discovered = [
            {"league_id": 1907081848, "display_name": "The League", "year": 2026, "my_team_name": None},
            {"league_id": 1319324, "display_name": "Molnar Mania", "year": 2026, "my_team_name": None},
        ]
        merged, changes = discovery._merge_registry(existing, discovered)
        self.assertEqual(merged["default_league"], "the_league")
        self.assertEqual(merged["leagues"]["the_league"]["league_id"], 1907081848)
        self.assertEqual(merged["leagues"]["molnar_mania"]["league_id"], 1319324)
        self.assertEqual(changes, [{
            "action": "added",
            "alias": "molnar_mania",
            "league_id": 1319324,
            "display_name": "Molnar Mania",
        }])

    def test_new_registry_gets_valid_default(self):
        discovered = [
            {"league_id": 320168, "display_name": "Hunt Ball", "year": 2026, "my_team_name": None},
        ]
        merged, changes = discovery._merge_registry(None, discovered)
        self.assertEqual(merged["version"], 1)
        self.assertEqual(merged["default_league"], "hunt_ball")
        self.assertEqual(changes[0]["alias"], "hunt_ball")


class DiscoveryTests(unittest.TestCase):
    @patch.object(discovery, "_verify_league")
    @patch.object(discovery, "_fetch_fan_profile")
    @patch.object(discovery, "_resolve_credentials")
    def test_discovery_verifies_candidates_and_returns_sorted_results(
        self, resolve_credentials, fetch_profile, verify_league
    ):
        resolve_credentials.return_value = ("fake-s2", "{fake-swid}", "environment")
        fetch_profile.return_value = {
            "items": [
                {"leagueId": 1319324, "game": "ffl"},
                {"leagueId": 320168, "game": "ffl"},
            ]
        }
        verify_league.side_effect = [
            {"league_id": 320168, "display_name": "Hunt Ball", "year": 2026, "my_team_name": "TN Outsiders"},
            {"league_id": 1319324, "display_name": "Molnar Mania", "year": 2026, "my_team_name": "Team M"},
        ]
        result = discovery.discover_espn_leagues(2026)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["league_count"], 2)
        self.assertEqual([row["display_name"] for row in result["leagues"]], ["Hunt Ball", "Molnar Mania"])
        self.assertNotIn("fake-s2", json.dumps(result))
        self.assertNotIn("fake-swid", json.dumps(result))


class SyncTests(unittest.TestCase):
    @patch.object(discovery, "discover_espn_leagues")
    def test_sync_preview_does_not_write(self, discover_mock):
        discover_mock.return_value = {
            "status": "ok",
            "year": 2026,
            "league_count": 1,
            "leagues": [{"league_id": 1319324, "display_name": "Molnar Mania", "year": 2026, "my_team_name": None}],
        }
        with TemporaryDirectory() as d:
            with patch.dict(os.environ, {"FANTASY_FOOTBALL_MCP_HOME": d}, clear=False):
                result = discovery.sync_espn_leagues(2026, confirm=False)
                self.assertFalse(Path(d, "league_registry.json").exists())
        self.assertFalse(result["write_performed"])
        self.assertTrue(result["confirmation_required"])

    @patch.object(discovery, "discover_espn_leagues")
    def test_confirmed_sync_writes_valid_registry(self, discover_mock):
        discover_mock.return_value = {
            "status": "ok",
            "year": 2026,
            "league_count": 1,
            "leagues": [{"league_id": 1319324, "display_name": "Molnar Mania", "year": 2026, "my_team_name": None}],
        }
        with TemporaryDirectory() as d:
            with patch.dict(os.environ, {"FANTASY_FOOTBALL_MCP_HOME": d}, clear=False):
                result = discovery.sync_espn_leagues(2026, confirm=True)
                target = Path(d, "league_registry.json")
                self.assertTrue(target.exists())
                payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertTrue(result["write_performed"])
        self.assertEqual(payload["leagues"]["molnar_mania"]["league_id"], 1319324)


class CredentialCompatibilityTests(unittest.TestCase):
    def test_quick_desktop_swid_alias_is_accepted(self):
        with patch.dict(os.environ, {"ESPN_S2": "fake-s2", "SWID": "{fake-swid}"}, clear=True):
            result = discovery._resolve_credentials()
        self.assertEqual(result, ("fake-s2", "{fake-swid}", "environment"))

    def test_conflicting_swid_names_fail_closed(self):
        with patch.dict(
            os.environ,
            {"ESPN_S2": "fake-s2", "ESPN_SWID": "{one}", "SWID": "{two}"},
            clear=True,
        ):
            with self.assertRaises(Exception):
                discovery._resolve_credentials()


if __name__ == "__main__":
    unittest.main()
