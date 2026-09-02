"""Deterministic, fully offline tests for the D3D-B non-secret user-state
relocation: league_registry.py, commissioner_config.py, and
draft_strategy_store.py's new app-home default location, legacy
source-relative read fallback, and explicit-path-override behavior.

Standard library only (unittest + tempfile + unittest.mock + pathlib +
json). NEVER touches the real user home, the real ~/.fantasy-football-mcp/,
or the real repo-relative legacy files (league_registry.json,
commissioner_config.json, .draft_strategy/) - every test patches
app_config.get_app_home() and/or the module-level LEGACY_*_PATH constants
to point into isolated tempfile.TemporaryDirectory() locations. All league
IDs, aliases, and strategy content in this file are synthetic fixtures
only.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_config
import league_registry
import commissioner_config
import draft_strategy_store as dss


# ---------------------------------------------------------------------
# Section 1: app_config new state-path helpers
# ---------------------------------------------------------------------

class TestAppConfigStatePaths(unittest.TestCase):
    def test_default_league_registry_path(self):
        fake_home = Path("/fake/home/dir")
        with patch.object(app_config.Path, "home", return_value=fake_home):
            with patch.dict(os.environ, {}, clear=True):
                result = app_config.get_league_registry_path()
        self.assertEqual(result, fake_home / ".fantasy-football-mcp" / "league_registry.json")

    def test_default_commissioner_config_path(self):
        fake_home = Path("/fake/home/dir")
        with patch.object(app_config.Path, "home", return_value=fake_home):
            with patch.dict(os.environ, {}, clear=True):
                result = app_config.get_commissioner_config_path()
        self.assertEqual(result, fake_home / ".fantasy-football-mcp" / "commissioner_config.json")

    def test_default_draft_strategy_dir(self):
        fake_home = Path("/fake/home/dir")
        with patch.object(app_config.Path, "home", return_value=fake_home):
            with patch.dict(os.environ, {}, clear=True):
                result = app_config.get_draft_strategy_dir()
        self.assertEqual(result, fake_home / ".fantasy-football-mcp" / "draft_strategy")

    def test_env_override_redirects_all_three(self):
        with patch.dict(os.environ, {"FANTASY_FOOTBALL_MCP_HOME": "/custom/ffm/home"}):
            reg = app_config.get_league_registry_path()
            com = app_config.get_commissioner_config_path()
            draft = app_config.get_draft_strategy_dir()
        self.assertEqual(reg, Path("/custom/ffm/home/league_registry.json"))
        self.assertEqual(com, Path("/custom/ffm/home/commissioner_config.json"))
        self.assertEqual(draft, Path("/custom/ffm/home/draft_strategy"))

    def test_resolving_paths_creates_no_filesystem_objects(self):
        with TemporaryDirectory() as d:
            fake_home = Path(d) / "does_not_exist_yet"
            with patch.dict(os.environ, {"FANTASY_FOOTBALL_MCP_HOME": str(fake_home)}):
                app_config.get_league_registry_path()
                app_config.get_commissioner_config_path()
                app_config.get_draft_strategy_dir()
            self.assertFalse(fake_home.exists())


# ---------------------------------------------------------------------
# Section 2: league_registry.py
# ---------------------------------------------------------------------

SYNTHETIC_REGISTRY = {
    "version": 1,
    "default_league": "synthetic_alias",
    "leagues": {
        "synthetic_alias": {"league_id": 111111111, "display_name": "Synthetic League", "enabled": True}
    },
}
SYNTHETIC_REGISTRY_LEGACY = {
    "version": 1,
    "default_league": "legacy_alias",
    "leagues": {
        "legacy_alias": {"league_id": 222222222, "display_name": "Legacy Synthetic League", "enabled": True}
    },
}


class TestLeagueRegistryRelocation(unittest.TestCase):
    def _write(self, path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_explicit_path_works(self):
        with TemporaryDirectory() as d:
            explicit_path = Path(d) / "explicit_registry.json"
            self._write(explicit_path, SYNTHETIC_REGISTRY)
            result = league_registry.load_registry(str(explicit_path))
        self.assertEqual(result["default_league"], "synthetic_alias")

    def test_explicit_missing_path_does_not_fallback(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_path = Path(new_d) / "league_registry.json"
            legacy_path = Path(legacy_d) / "league_registry.json"
            self._write(new_path, SYNTHETIC_REGISTRY)
            self._write(legacy_path, SYNTHETIC_REGISTRY_LEGACY)
            missing_explicit = Path(new_d) / "does_not_exist.json"
            with patch.object(app_config, "get_league_registry_path", return_value=new_path):
                with patch.object(league_registry, "LEGACY_REGISTRY_PATH", str(legacy_path)):
                    with self.assertRaises(league_registry.RegistryError):
                        league_registry.load_registry(str(missing_explicit))

    def test_new_default_path_used_when_present(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_path = Path(new_d) / "league_registry.json"
            legacy_path = Path(legacy_d) / "league_registry.json"
            self._write(new_path, SYNTHETIC_REGISTRY)
            with patch.object(app_config, "get_league_registry_path", return_value=new_path):
                with patch.object(league_registry, "LEGACY_REGISTRY_PATH", str(legacy_path)):
                    result = league_registry.load_registry()
        self.assertEqual(result["default_league"], "synthetic_alias")

    def test_legacy_fallback_when_new_absent(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_path = Path(new_d) / "league_registry.json"  # never created
            legacy_path = Path(legacy_d) / "league_registry.json"
            self._write(legacy_path, SYNTHETIC_REGISTRY_LEGACY)
            with patch.object(app_config, "get_league_registry_path", return_value=new_path):
                with patch.object(league_registry, "LEGACY_REGISTRY_PATH", str(legacy_path)):
                    result = league_registry.load_registry()
        self.assertEqual(result["default_league"], "legacy_alias")

    def test_new_wins_when_both_exist(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_path = Path(new_d) / "league_registry.json"
            legacy_path = Path(legacy_d) / "league_registry.json"
            self._write(new_path, SYNTHETIC_REGISTRY)
            self._write(legacy_path, SYNTHETIC_REGISTRY_LEGACY)
            with patch.object(app_config, "get_league_registry_path", return_value=new_path):
                with patch.object(league_registry, "LEGACY_REGISTRY_PATH", str(legacy_path)):
                    result = league_registry.load_registry()
        self.assertEqual(result["default_league"], "synthetic_alias")

    def test_malformed_new_does_not_fallback_to_valid_legacy(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_path = Path(new_d) / "league_registry.json"
            legacy_path = Path(legacy_d) / "league_registry.json"
            new_path.parent.mkdir(parents=True, exist_ok=True)
            new_path.write_text("{not valid json", encoding="utf-8")
            self._write(legacy_path, SYNTHETIC_REGISTRY_LEGACY)
            with patch.object(app_config, "get_league_registry_path", return_value=new_path):
                with patch.object(league_registry, "LEGACY_REGISTRY_PATH", str(legacy_path)):
                    with self.assertRaises(league_registry.RegistryError):
                        league_registry.load_registry()

    def test_neither_exists_preserves_missing_error_behavior(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_path = Path(new_d) / "league_registry.json"
            legacy_path = Path(legacy_d) / "league_registry.json"
            with patch.object(app_config, "get_league_registry_path", return_value=new_path):
                with patch.object(league_registry, "LEGACY_REGISTRY_PATH", str(legacy_path)):
                    with self.assertRaises(league_registry.RegistryError):
                        league_registry.load_registry()

    def test_validation_behavior_unchanged(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "registry.json"
            self._write(path, {"version": 1, "default_league": "nope", "leagues": {}})
            with self.assertRaises(league_registry.RegistryError):
                league_registry.load_registry(str(path))

    def test_read_creates_no_directories(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_home = Path(new_d) / "not_yet_created"
            new_path = new_home / "league_registry.json"
            legacy_path = Path(legacy_d) / "league_registry.json"
            self._write(legacy_path, SYNTHETIC_REGISTRY_LEGACY)
            with patch.object(app_config, "get_league_registry_path", return_value=new_path):
                with patch.object(league_registry, "LEGACY_REGISTRY_PATH", str(legacy_path)):
                    league_registry.load_registry()
            self.assertFalse(new_home.exists())


# ---------------------------------------------------------------------
# Section 3: commissioner_config.py
# ---------------------------------------------------------------------

SYNTHETIC_COMMISSIONER = {"version": 1, "leagues": {"synthetic_alias": {"league_id": 111111111, "enabled": True}}}
SYNTHETIC_COMMISSIONER_LEGACY = {"version": 1, "leagues": {"legacy_alias": {"league_id": 222222222, "enabled": True}}}


class TestCommissionerConfigRelocation(unittest.TestCase):
    def _write(self, path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_explicit_path_works(self):
        with TemporaryDirectory() as d:
            explicit_path = Path(d) / "explicit_commissioner.json"
            self._write(explicit_path, SYNTHETIC_COMMISSIONER)
            result = commissioner_config.load_config(str(explicit_path))
        self.assertIn("synthetic_alias", result["leagues"])

    def test_explicit_missing_path_does_not_fallback(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_path = Path(new_d) / "commissioner_config.json"
            legacy_path = Path(legacy_d) / "commissioner_config.json"
            self._write(new_path, SYNTHETIC_COMMISSIONER)
            self._write(legacy_path, SYNTHETIC_COMMISSIONER_LEGACY)
            missing_explicit = Path(new_d) / "does_not_exist.json"
            with patch.object(app_config, "get_commissioner_config_path", return_value=new_path):
                with patch.object(commissioner_config, "LEGACY_CONFIG_PATH", str(legacy_path)):
                    with self.assertRaises(commissioner_config.CommissionerConfigError):
                        commissioner_config.load_config(str(missing_explicit))

    def test_new_default_used_when_present(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_path = Path(new_d) / "commissioner_config.json"
            legacy_path = Path(legacy_d) / "commissioner_config.json"
            self._write(new_path, SYNTHETIC_COMMISSIONER)
            with patch.object(app_config, "get_commissioner_config_path", return_value=new_path):
                with patch.object(commissioner_config, "LEGACY_CONFIG_PATH", str(legacy_path)):
                    result = commissioner_config.load_config()
        self.assertIn("synthetic_alias", result["leagues"])

    def test_legacy_fallback(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_path = Path(new_d) / "commissioner_config.json"
            legacy_path = Path(legacy_d) / "commissioner_config.json"
            self._write(legacy_path, SYNTHETIC_COMMISSIONER_LEGACY)
            with patch.object(app_config, "get_commissioner_config_path", return_value=new_path):
                with patch.object(commissioner_config, "LEGACY_CONFIG_PATH", str(legacy_path)):
                    result = commissioner_config.load_config()
        self.assertIn("legacy_alias", result["leagues"])

    def test_new_wins_when_both_exist(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_path = Path(new_d) / "commissioner_config.json"
            legacy_path = Path(legacy_d) / "commissioner_config.json"
            self._write(new_path, SYNTHETIC_COMMISSIONER)
            self._write(legacy_path, SYNTHETIC_COMMISSIONER_LEGACY)
            with patch.object(app_config, "get_commissioner_config_path", return_value=new_path):
                with patch.object(commissioner_config, "LEGACY_CONFIG_PATH", str(legacy_path)):
                    result = commissioner_config.load_config()
        self.assertIn("synthetic_alias", result["leagues"])
        self.assertNotIn("legacy_alias", result["leagues"])

    def test_malformed_new_does_not_fallback(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_path = Path(new_d) / "commissioner_config.json"
            legacy_path = Path(legacy_d) / "commissioner_config.json"
            new_path.parent.mkdir(parents=True, exist_ok=True)
            new_path.write_text("{not valid json", encoding="utf-8")
            self._write(legacy_path, SYNTHETIC_COMMISSIONER_LEGACY)
            with patch.object(app_config, "get_commissioner_config_path", return_value=new_path):
                with patch.object(commissioner_config, "LEGACY_CONFIG_PATH", str(legacy_path)):
                    with self.assertRaises(commissioner_config.CommissionerConfigError):
                        commissioner_config.load_config()

    def test_neither_exists_error(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_path = Path(new_d) / "commissioner_config.json"
            legacy_path = Path(legacy_d) / "commissioner_config.json"
            with patch.object(app_config, "get_commissioner_config_path", return_value=new_path):
                with patch.object(commissioner_config, "LEGACY_CONFIG_PATH", str(legacy_path)):
                    with self.assertRaises(commissioner_config.CommissionerConfigError):
                        commissioner_config.load_config()

    def test_out_of_scope_key_protection_unchanged(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "commissioner_config.json"
            self._write(path, {"version": 1, "leagues": {"a": {"league_id": 1, "write_authorized": True}}})
            with self.assertRaises(commissioner_config.CommissionerConfigError):
                commissioner_config.load_config(str(path))

    def test_read_creates_no_directories(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_home = Path(new_d) / "not_yet_created"
            new_path = new_home / "commissioner_config.json"
            legacy_path = Path(legacy_d) / "commissioner_config.json"
            self._write(legacy_path, SYNTHETIC_COMMISSIONER_LEGACY)
            with patch.object(app_config, "get_commissioner_config_path", return_value=new_path):
                with patch.object(commissioner_config, "LEGACY_CONFIG_PATH", str(legacy_path)):
                    commissioner_config.load_config()
            self.assertFalse(new_home.exists())


# ---------------------------------------------------------------------
# Section 4: draft_strategy_store.py
# ---------------------------------------------------------------------

def _make_doc(league_id, year, marker):
    return {
        "schema_version": 2,
        "league_id": league_id,
        "year": year,
        "strategy_id": f"synthetic-{marker}",
        "input_fingerprint": f"fp-{marker}",
        "created_at_utc": "2026-01-01T00:00:00+00:00",
        "structural_inputs": {"marker": marker},
        "structural_fingerprint": dss._recompute_structural_fingerprint({"marker": marker}),
    }


class TestDraftStrategyRelocation(unittest.TestCase):
    LEAGUE_ID = 333333333
    YEAR = 2027

    def test_explicit_strategy_dir_write_and_read(self):
        with TemporaryDirectory() as d:
            strategy_dir = Path(d) / "explicit"
            doc = _make_doc(self.LEAGUE_ID, self.YEAR, "explicit")
            dss.save_strategy(self.LEAGUE_ID, self.YEAR, doc, strategy_dir=strategy_dir)
            result = dss.load_strategy(self.LEAGUE_ID, self.YEAR, strategy_dir=strategy_dir)
        self.assertEqual(result["strategy_id"], "synthetic-explicit")

    def test_explicit_missing_strategy_dir_does_not_fallback(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d, TemporaryDirectory() as explicit_d:
            new_dir = Path(new_d)
            legacy_dir = Path(legacy_d)
            doc = _make_doc(self.LEAGUE_ID, self.YEAR, "legacy")
            dss.save_strategy(self.LEAGUE_ID, self.YEAR, doc, strategy_dir=legacy_dir)
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                with patch.object(dss, "LEGACY_STORE_DIR", legacy_dir):
                    result = dss.load_strategy(self.LEAGUE_ID, self.YEAR, strategy_dir=Path(explicit_d) / "empty")
        self.assertIsNone(result)

    def test_new_default_read_works(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir = Path(new_d)
            legacy_dir = Path(legacy_d)
            doc = _make_doc(self.LEAGUE_ID, self.YEAR, "new")
            dss.save_strategy(self.LEAGUE_ID, self.YEAR, doc, strategy_dir=new_dir)
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                with patch.object(dss, "LEGACY_STORE_DIR", legacy_dir):
                    result = dss.load_strategy(self.LEAGUE_ID, self.YEAR)
        self.assertEqual(result["strategy_id"], "synthetic-new")

    def test_legacy_fallback_read_works(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir = Path(new_d)
            legacy_dir = Path(legacy_d)
            doc = _make_doc(self.LEAGUE_ID, self.YEAR, "legacy")
            dss.save_strategy(self.LEAGUE_ID, self.YEAR, doc, strategy_dir=legacy_dir)
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                with patch.object(dss, "LEGACY_STORE_DIR", legacy_dir):
                    result = dss.load_strategy(self.LEAGUE_ID, self.YEAR)
        self.assertEqual(result["strategy_id"], "synthetic-legacy")

    def test_new_wins_when_both_exist(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir = Path(new_d)
            legacy_dir = Path(legacy_d)
            dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "new"), strategy_dir=new_dir)
            dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "legacy"), strategy_dir=legacy_dir)
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                with patch.object(dss, "LEGACY_STORE_DIR", legacy_dir):
                    result = dss.load_strategy(self.LEAGUE_ID, self.YEAR)
        self.assertEqual(result["strategy_id"], "synthetic-new")

    def test_malformed_new_raises_valid_legacy_not_used(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir = Path(new_d)
            legacy_dir = Path(legacy_d)
            new_dir.mkdir(parents=True, exist_ok=True)
            (new_dir / f"{self.LEAGUE_ID}_{self.YEAR}.json").write_text("{not valid json", encoding="utf-8")
            dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "legacy"), strategy_dir=legacy_dir)
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                with patch.object(dss, "LEGACY_STORE_DIR", legacy_dir):
                    with self.assertRaises(dss.DraftStrategyStoreError):
                        dss.load_strategy(self.LEAGUE_ID, self.YEAR)

    def test_strategy_exists_follows_same_precedence(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir = Path(new_d)
            legacy_dir = Path(legacy_d)
            dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "legacy"), strategy_dir=legacy_dir)
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                with patch.object(dss, "LEGACY_STORE_DIR", legacy_dir):
                    self.assertTrue(dss.strategy_exists(self.LEAGUE_ID, self.YEAR))
                    self.assertFalse(dss.strategy_exists(self.LEAGUE_ID, self.YEAR + 999))

    def test_strategy_exists_creates_no_directories(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir = Path(new_d) / "not_yet_created"
            legacy_dir = Path(legacy_d) / "not_yet_created"
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                with patch.object(dss, "LEGACY_STORE_DIR", legacy_dir):
                    dss.strategy_exists(self.LEAGUE_ID, self.YEAR)
            self.assertFalse(new_dir.exists())
            self.assertFalse(legacy_dir.exists())

    def test_load_creates_no_directories(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir = Path(new_d) / "not_yet_created"
            legacy_dir = Path(legacy_d) / "not_yet_created"
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                with patch.object(dss, "LEGACY_STORE_DIR", legacy_dir):
                    dss.load_strategy(self.LEAGUE_ID, self.YEAR)
            self.assertFalse(new_dir.exists())
            self.assertFalse(legacy_dir.exists())

    def test_default_save_creates_new_directory(self):
        with TemporaryDirectory() as new_d:
            new_dir = Path(new_d) / "not_yet_created"
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "x"))
            self.assertTrue(new_dir.exists())

    def test_default_save_writes_to_new_location(self):
        with TemporaryDirectory() as new_d:
            new_dir = Path(new_d)
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                final_path = dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "x"))
            self.assertEqual(final_path.parent, new_dir)
            self.assertTrue((new_dir / f"{self.LEAGUE_ID}_{self.YEAR}.json").exists())

    def test_default_save_does_not_modify_existing_legacy_file(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir = Path(new_d)
            legacy_dir = Path(legacy_d)
            legacy_path = dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "legacy"), strategy_dir=legacy_dir)
            before_bytes = legacy_path.read_bytes()
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                with patch.object(dss, "LEGACY_STORE_DIR", legacy_dir):
                    dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "new"))
            after_bytes = legacy_path.read_bytes()
        self.assertEqual(before_bytes, after_bytes)

    def test_only_legacy_exists_save_does_not_implicitly_copy_legacy_content(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir = Path(new_d)
            legacy_dir = Path(legacy_d)
            dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "legacy"), strategy_dir=legacy_dir)
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                with patch.object(dss, "LEGACY_STORE_DIR", legacy_dir):
                    dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "brand-new-doc"))
                    new_result = dss.load_strategy(self.LEAGUE_ID, self.YEAR)
        self.assertEqual(new_result["strategy_id"], "synthetic-brand-new-doc")

    def test_atomic_write_still_works(self):
        with TemporaryDirectory() as new_d:
            new_dir = Path(new_d)
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                final_path = dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "atomic"))
            leftover_tmp = list(new_dir.glob(".*_*.tmp"))
            self.assertEqual(leftover_tmp, [])
            self.assertTrue(final_path.exists())

    def test_secret_shaped_key_rejection_still_works(self):
        with TemporaryDirectory() as new_d:
            new_dir = Path(new_d)
            bad_doc = _make_doc(self.LEAGUE_ID, self.YEAR, "bad")
            bad_doc["espn_s2"] = "should-never-be-persisted"
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                with self.assertRaises(dss.DraftStrategyStoreError):
                    dss.save_strategy(self.LEAGUE_ID, self.YEAR, bad_doc)
            self.assertEqual(list(new_dir.glob("*.json")), [])

    def test_filename_semantics_unchanged(self):
        with TemporaryDirectory() as new_d:
            new_dir = Path(new_d)
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_dir):
                final_path = dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "x"))
            self.assertEqual(final_path.name, f"{self.LEAGUE_ID}_{self.YEAR}.json")


# ---------------------------------------------------------------------
# Section 5: full backward-compatibility migration scenario
# ---------------------------------------------------------------------

class TestBackwardCompatibilityMigrationScenario(unittest.TestCase):
    """Simulates an existing legacy-only installation upgrading to D3D-B,
    per the release-critical migration scenario."""

    LEAGUE_ID = 444444444
    YEAR = 2028

    def test_legacy_only_installation_then_new_write_migrates_forward(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir = Path(new_d)  # empty app-home; nothing exists yet
            legacy_dir = Path(legacy_d)

            legacy_registry_path = legacy_dir / "league_registry.json"
            legacy_registry_path.write_text(json.dumps(SYNTHETIC_REGISTRY_LEGACY), encoding="utf-8")
            legacy_commissioner_path = legacy_dir / "commissioner_config.json"
            legacy_commissioner_path.write_text(json.dumps(SYNTHETIC_COMMISSIONER_LEGACY), encoding="utf-8")
            legacy_strategy_dir = legacy_dir / "draft_strategy_legacy"
            dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "legacy"),
                               strategy_dir=legacy_strategy_dir)
            legacy_strategy_file = legacy_strategy_dir / f"{self.LEAGUE_ID}_{self.YEAR}.json"
            legacy_bytes_before = legacy_strategy_file.read_bytes()

            new_registry_path = new_dir / "league_registry.json"
            new_commissioner_path = new_dir / "commissioner_config.json"
            new_strategy_dir = new_dir / "draft_strategy_new"

            with patch.object(app_config, "get_league_registry_path", return_value=new_registry_path):
                with patch.object(league_registry, "LEGACY_REGISTRY_PATH", str(legacy_registry_path)):
                    registry = league_registry.load_registry()
            self.assertEqual(registry["default_league"], "legacy_alias")

            with patch.object(app_config, "get_commissioner_config_path", return_value=new_commissioner_path):
                with patch.object(commissioner_config, "LEGACY_CONFIG_PATH", str(legacy_commissioner_path)):
                    config = commissioner_config.load_config()
            self.assertIn("legacy_alias", config["leagues"])

            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_strategy_dir):
                with patch.object(dss, "LEGACY_STORE_DIR", legacy_strategy_dir):
                    strategy = dss.load_strategy(self.LEAGUE_ID, self.YEAR)
                    self.assertEqual(strategy["strategy_id"], "synthetic-legacy")

                    # Now save a NEW strategy - must go to the new directory.
                    dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "migrated"))

                    self.assertTrue(new_strategy_dir.exists())
                    new_strategy_file = new_strategy_dir / f"{self.LEAGUE_ID}_{self.YEAR}.json"
                    self.assertTrue(new_strategy_file.exists())

                    self.assertEqual(legacy_strategy_file.read_bytes(), legacy_bytes_before)

                    subsequent_read = dss.load_strategy(self.LEAGUE_ID, self.YEAR)
                    self.assertEqual(subsequent_read["strategy_id"], "synthetic-migrated")


# ---------------------------------------------------------------------
# Section 6: clean-clone scenario (no legacy, no new state)
# ---------------------------------------------------------------------

class TestCleanCloneScenario(unittest.TestCase):
    LEAGUE_ID = 555555555
    YEAR = 2029

    def test_clean_clone_no_state_anywhere(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir = Path(new_d) / "app_home_not_created"
            legacy_registry_path = Path(legacy_d) / "league_registry.json"  # never created
            legacy_commissioner_path = Path(legacy_d) / "commissioner_config.json"  # never created
            legacy_strategy_dir = Path(legacy_d) / "draft_strategy_legacy"  # never created

            with patch.object(app_config, "get_league_registry_path", return_value=new_dir / "league_registry.json"):
                with patch.object(league_registry, "LEGACY_REGISTRY_PATH", str(legacy_registry_path)):
                    with self.assertRaises(league_registry.RegistryError):
                        league_registry.load_registry()

            with patch.object(app_config, "get_commissioner_config_path", return_value=new_dir / "commissioner_config.json"):
                with patch.object(commissioner_config, "LEGACY_CONFIG_PATH", str(legacy_commissioner_path)):
                    with self.assertRaises(commissioner_config.CommissionerConfigError):
                        commissioner_config.load_config()

            new_strategy_dir = new_dir / "draft_strategy_new"
            with patch.object(app_config, "get_draft_strategy_dir", return_value=new_strategy_dir):
                with patch.object(dss, "LEGACY_STORE_DIR", legacy_strategy_dir):
                    self.assertIsNone(dss.load_strategy(self.LEAGUE_ID, self.YEAR))
                    self.assertFalse(dss.strategy_exists(self.LEAGUE_ID, self.YEAR))

                    self.assertFalse(new_strategy_dir.exists())
                    self.assertFalse(legacy_strategy_dir.exists())

                    # First save creates ONLY the new directory.
                    dss.save_strategy(self.LEAGUE_ID, self.YEAR, _make_doc(self.LEAGUE_ID, self.YEAR, "first"))
                    self.assertTrue(new_strategy_dir.exists())
                    self.assertFalse(legacy_strategy_dir.exists())


if __name__ == "__main__":
    unittest.main()
