"""Deterministic, fully offline tests for the D4B FantasyPros cache
relocation: new app-home default location, per-file legacy read fallback,
write-forward-only semantics, quota/usage-ledger migration safety, and
read-only-install safety.

Standard library only (unittest + tempfile + unittest.mock + pathlib +
json). NEVER touches the real user home, the real ~/.fantasy-football-mcp/,
or the developer's real .fp_cache/ - every test patches
app_config.get_fantasypros_cache_dir() and/or fantasypros_client's
LEGACY_CACHE_DIR to point into isolated tempfile.TemporaryDirectory()
locations. All FantasyPros data used here is synthetic fixture content
only - no real API keys, no real cache content copied from the developer's
machine.
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
import fantasypros_client as fp


# ---------------------------------------------------------------------
# Section 1: app_config new path helper
# ---------------------------------------------------------------------

class TestAppConfigFpCachePath(unittest.TestCase):
    def test_default_cache_path(self):
        fake_home = Path("/fake/home/dir")
        with patch.object(app_config.Path, "home", return_value=fake_home):
            with patch.dict(os.environ, {}, clear=True):
                result = app_config.get_fantasypros_cache_dir()
        self.assertEqual(result, fake_home / ".fantasy-football-mcp" / "fp_cache")

    def test_env_override(self):
        with patch.dict(os.environ, {"FANTASY_FOOTBALL_MCP_HOME": "/custom/ffm/home"}):
            result = app_config.get_fantasypros_cache_dir()
        self.assertEqual(result, Path("/custom/ffm/home/fp_cache"))

    def test_resolving_creates_nothing(self):
        with TemporaryDirectory() as d:
            fake_home = Path(d) / "does_not_exist_yet"
            with patch.dict(os.environ, {"FANTASY_FOOTBALL_MCP_HOME": str(fake_home)}):
                app_config.get_fantasypros_cache_dir()
            self.assertFalse(fake_home.exists())


# ---------------------------------------------------------------------
# Section 2: per-file read precedence (representative cache artifacts)
# ---------------------------------------------------------------------

def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class TestPerFileReadPrecedence(unittest.TestCase):
    """Covers _resolve_cache_read_path (shared by _usage_path and
    _cache_path) representatively via _cache_path, plus one dedicated
    pass for _usage_path since the quota ledger has its own filename."""

    def test_new_used_when_present(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            _write_json(new_dir / "players.json", {"marker": "new"})
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    self.assertEqual(fp._cache_path("players"), new_dir / "players.json")

    def test_legacy_used_when_new_absent(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            _write_json(legacy_dir / "players.json", {"marker": "legacy"})
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    self.assertEqual(fp._cache_path("players"), legacy_dir / "players.json")

    def test_new_wins_when_both_exist(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            _write_json(new_dir / "players.json", {"marker": "new"})
            _write_json(legacy_dir / "players.json", {"marker": "legacy"})
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    self.assertEqual(fp._cache_path("players"), new_dir / "players.json")

    def test_neither_exists_behaves_as_before(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    result = fp._read_cache("players")
        self.assertIsNone(result)

    def test_malformed_new_does_not_fallback_to_valid_legacy(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            new_dir.mkdir(parents=True, exist_ok=True)
            (new_dir / "players.json").write_text("{not valid json", encoding="utf-8")
            _write_json(legacy_dir / "players.json", {"marker": "legacy", "record_count": 1})
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    # _read_cache catches JSONDecodeError and returns None
                    # (preserving existing malformed-cache semantics) -
                    # critically, it must NOT return the legacy dict.
                    result = fp._read_cache("players")
        self.assertIsNone(result)

    def test_rankings_dataset_key_representative(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            _write_json(legacy_dir / "rankings_QB_PPR.json", {"marker": "legacy_rankings"})
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    self.assertEqual(fp._cache_path("rankings_QB_PPR"), legacy_dir / "rankings_QB_PPR.json")

    def test_usage_ledger_new_used_when_present(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            _write_json(new_dir / "request_usage.json", {"marker": "new"})
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    self.assertEqual(fp._usage_path(), new_dir / "request_usage.json")

    def test_usage_ledger_legacy_used_when_new_absent(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            _write_json(legacy_dir / "request_usage.json", {"marker": "legacy"})
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    self.assertEqual(fp._usage_path(), legacy_dir / "request_usage.json")


# ---------------------------------------------------------------------
# Section 3: quota/usage ledger migration safety (release-critical)
# ---------------------------------------------------------------------

class TestQuotaLedgerMigration(unittest.TestCase):
    def test_new_ledger_exists_uses_new(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            today = fp._today_str()
            _write_json(new_dir / "request_usage.json",
                        {"date": today, "requests_made_today": 42, "last_request_at": None})
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    usage = fp._load_usage()
        self.assertEqual(usage["requests_made_today"], 42)

    def test_only_legacy_usage_ledger_loads_legacy(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            today = fp._today_str()
            _write_json(legacy_dir / "request_usage.json",
                        {"date": today, "requests_made_today": 7, "last_request_at": None})
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    usage = fp._load_usage()
        self.assertEqual(usage["requests_made_today"], 7)

    def test_both_exist_uses_new(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            today = fp._today_str()
            _write_json(new_dir / "request_usage.json",
                        {"date": today, "requests_made_today": 10, "last_request_at": None})
            _write_json(legacy_dir / "request_usage.json",
                        {"date": today, "requests_made_today": 999, "last_request_at": None})
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    usage = fp._load_usage()
        self.assertEqual(usage["requests_made_today"], 10)

    def test_malformed_new_does_not_fallback(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            today = fp._today_str()
            new_dir.mkdir(parents=True, exist_ok=True)
            (new_dir / "request_usage.json").write_text("{not valid json", encoding="utf-8")
            _write_json(legacy_dir / "request_usage.json",
                        {"date": today, "requests_made_today": 999, "last_request_at": None})
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    usage = fp._load_usage()
        # Malformed new -> treated as unreadable -> resets to a FRESH
        # ledger (0), never falls back to legacy's 999.
        self.assertEqual(usage["requests_made_today"], 0)

    def test_legacy_only_update_migrates_forward_legacy_untouched(self):
        """Release-critical: load old usage count/state from legacy,
        update it, write result to NEW location, legacy file remains
        byte-identical."""
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            today = fp._today_str()
            legacy_path = legacy_dir / "request_usage.json"
            _write_json(legacy_path, {"date": today, "requests_made_today": 5, "last_request_at": None})
            legacy_bytes_before = legacy_path.read_bytes()

            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    updated = fp._increment_usage()

                    self.assertEqual(updated["requests_made_today"], 6)  # 5 -> 6, starting state preserved

                    new_path = new_dir / "request_usage.json"
                    self.assertTrue(new_path.exists())
                    with open(new_path, encoding="utf-8") as f:
                        new_content = json.load(f)
                    self.assertEqual(new_content["requests_made_today"], 6)

                    self.assertEqual(legacy_path.read_bytes(), legacy_bytes_before)

                    # Subsequent read now prefers the new artifact.
                    subsequent = fp._load_usage()
        self.assertEqual(subsequent["requests_made_today"], 6)

    def test_no_ledger_anywhere_preserves_default_empty_behavior(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    usage = fp._load_usage()
        self.assertEqual(usage["requests_made_today"], 0)
        self.assertEqual(usage["date"], fp._today_str())


# ---------------------------------------------------------------------
# Section 4: write destination
# ---------------------------------------------------------------------

class TestWriteDestination(unittest.TestCase):
    def test_new_cache_write_creates_new_dir(self):
        with TemporaryDirectory() as new_d:
            new_dir = Path(new_d) / "not_yet_created"
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                fp._atomic_write_json(fp._cache_write_path("players"), {"record_count": 0})
            self.assertTrue(new_dir.exists())
            self.assertTrue((new_dir / "players.json").exists())

    def test_write_lands_only_in_new_location(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    fp._atomic_write_json(fp._cache_write_path("players"), {"record_count": 1})
            self.assertTrue((new_dir / "players.json").exists())
            self.assertFalse((legacy_dir / "players.json").exists())

    def test_existing_legacy_only_cache_does_not_cause_writes_back_into_legacy(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            legacy_path = legacy_dir / "players.json"
            _write_json(legacy_path, {"marker": "legacy"})
            legacy_bytes_before = legacy_path.read_bytes()
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    fp._atomic_write_json(fp._cache_write_path("players"), {"marker": "brand_new"})
            self.assertEqual(legacy_path.read_bytes(), legacy_bytes_before)

    def test_write_creates_directory_only_when_needed(self):
        with TemporaryDirectory() as new_d:
            new_dir = Path(new_d) / "not_yet_created"
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                # Resolving alone must not create anything.
                fp._cache_path("players")
                self.assertFalse(new_dir.exists())
                # Only an actual write creates it.
                fp._atomic_write_json(fp._cache_write_path("players"), {"record_count": 0})
                self.assertTrue(new_dir.exists())

    def test_read_does_not_create_directory(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir = Path(new_d) / "not_yet_created"
            legacy_dir = Path(legacy_d) / "not_yet_created"
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    fp._read_cache("players")
            self.assertFalse(new_dir.exists())
            self.assertFalse(legacy_dir.exists())


# ---------------------------------------------------------------------
# Section 5: read-only package/module directory safety
# ---------------------------------------------------------------------

class TestReadOnlyInstallSafety(unittest.TestCase):
    def test_normal_write_never_touches_module_directory(self):
        """Simulates the module/source directory (LEGACY_CACHE_DIR's
        parent) being read-only by making its mkdir raise if called -
        proves a normal new-location write never attempts to create or
        touch anything under the legacy/module directory."""
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)

            real_mkdir = Path.mkdir

            def guarded_mkdir(self_path, *args, **kwargs):
                if legacy_dir in self_path.parents or self_path == legacy_dir:
                    raise AssertionError("attempted to mkdir under the legacy/module directory: %s" % self_path)
                return real_mkdir(self_path, *args, **kwargs)

            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    with patch.object(Path, "mkdir", guarded_mkdir):
                        fp._atomic_write_json(fp._cache_write_path("players"), {"record_count": 0})
            self.assertTrue((new_dir / "players.json").exists())

    def test_legacy_read_still_works_under_the_same_guard(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)
            _write_json(legacy_dir / "players.json", {"marker": "legacy", "record_count": 3})

            real_mkdir = Path.mkdir

            def guarded_mkdir(self_path, *args, **kwargs):
                if legacy_dir in self_path.parents or self_path == legacy_dir:
                    raise AssertionError("attempted to mkdir under the legacy/module directory: %s" % self_path)
                return real_mkdir(self_path, *args, **kwargs)

            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    with patch.object(Path, "mkdir", guarded_mkdir):
                        result = fp._read_cache("players")
        self.assertEqual(result["record_count"], 3)


# ---------------------------------------------------------------------
# Section 6: app-home write failure safety
# ---------------------------------------------------------------------

class TestAppHomeWriteFailureSafety(unittest.TestCase):
    FAKE_KEY = "TEST_SECRET_DO_NOT_PRINT_API_KEY"

    def test_permission_error_on_mkdir_raises_safe_cache_error(self):
        with TemporaryDirectory() as new_d:
            new_dir = Path(new_d) / "unwritable"
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
                    with self.assertRaises(fp.FantasyProsCacheError) as ctx:
                        fp._atomic_write_json(fp._cache_write_path("players"), {"record_count": 0, "api_key": self.FAKE_KEY})
            self.assertIn(str(new_dir), str(ctx.exception))
            self.assertNotIn(self.FAKE_KEY, str(ctx.exception))

    def test_permission_error_on_mkstemp_raises_safe_cache_error(self):
        with TemporaryDirectory() as new_d:
            new_dir = Path(new_d)
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch("tempfile.mkstemp", side_effect=PermissionError("denied")):
                    with self.assertRaises(fp.FantasyProsCacheError) as ctx:
                        fp._atomic_write_json(fp._cache_write_path("players"), {"record_count": 0, "api_key": self.FAKE_KEY})
            self.assertNotIn(self.FAKE_KEY, str(ctx.exception))

    def test_no_cached_payload_content_in_error(self):
        with TemporaryDirectory() as new_d:
            new_dir = Path(new_d) / "unwritable"
            payload = {"record_count": 0, "sensitive_marker": "TEST_SECRET_PAYLOAD_MARKER"}
            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
                    try:
                        fp._atomic_write_json(fp._cache_write_path("players"), payload)
                        self.fail("expected FantasyProsCacheError")
                    except fp.FantasyProsCacheError as e:
                        self.assertNotIn("TEST_SECRET_PAYLOAD_MARKER", str(e))


# ---------------------------------------------------------------------
# Section 7: existing freshness behavior unaffected
# ---------------------------------------------------------------------

class TestExistingFreshnessBehaviorUnaffected(unittest.TestCase):
    def test_fresh_cache_not_stale(self):
        import datetime
        fresh_obj = {"fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")}
        self.assertFalse(fp._is_stale(fresh_obj, ttl_seconds=3600))

    def test_stale_cache_detected(self):
        import datetime
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=10)).isoformat().replace("+00:00", "Z")
        stale_obj = {"fetched_at": old}
        self.assertTrue(fp._is_stale(stale_obj, ttl_seconds=3600))

    def test_missing_cache_object_is_stale(self):
        self.assertTrue(fp._is_stale(None, ttl_seconds=3600))

    def test_dataset_freshness_missing_status(self):
        result = fp._dataset_freshness(None, ttl_seconds=3600)
        self.assertEqual(result["status"], "missing")


# ---------------------------------------------------------------------
# Section 8: backward-compatibility migration scenario
# ---------------------------------------------------------------------

class TestBackwardCompatibilityMigrationScenario(unittest.TestCase):
    def test_legacy_only_installation_reads_succeed_then_write_migrates(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir, legacy_dir = Path(new_d), Path(legacy_d)  # new empty entirely
            today = fp._today_str()

            _write_json(legacy_dir / "players.json", {"marker": "legacy_players", "record_count": 100})
            _write_json(legacy_dir / "rankings_QB_PPR.json", {"marker": "legacy_rankings"})
            usage_path = legacy_dir / "request_usage.json"
            _write_json(usage_path, {"date": today, "requests_made_today": 12, "last_request_at": None})
            usage_bytes_before = usage_path.read_bytes()

            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    # Reads succeed from legacy, no migration merely from reading.
                    players = fp._read_cache("players")
                    self.assertEqual(players["marker"], "legacy_players")
                    rankings = fp._read_cache("rankings_QB_PPR")
                    self.assertEqual(rankings["marker"], "legacy_rankings")
                    usage = fp._load_usage()
                    self.assertEqual(usage["requests_made_today"], 12)
                    self.assertFalse((new_dir / "players.json").exists() if new_dir.exists() else False)

                    # First write/refresh goes to new app-home cache.
                    fp._atomic_write_json(fp._cache_write_path("players"), {"marker": "migrated_players", "record_count": 200})
                    fp._increment_usage()

                    self.assertTrue((new_dir / "players.json").exists())
                    self.assertEqual((legacy_dir / "players.json").read_bytes(),
                                      json.dumps({"marker": "legacy_players", "record_count": 100}).encode("utf-8"))
                    self.assertEqual(usage_path.read_bytes(), usage_bytes_before)

                    # Subsequent reads prefer the new written artifact.
                    subsequent_players = fp._read_cache("players")
                    self.assertEqual(subsequent_players["marker"], "migrated_players")
                    subsequent_usage = fp._load_usage()
                    self.assertEqual(subsequent_usage["requests_made_today"], 13)


# ---------------------------------------------------------------------
# Section 9: clean-install scenario
# ---------------------------------------------------------------------

class TestCleanInstallScenario(unittest.TestCase):
    def test_no_legacy_no_new_state_anywhere(self):
        with TemporaryDirectory() as new_d, TemporaryDirectory() as legacy_d:
            new_dir = Path(new_d) / "app_home_not_created"
            legacy_dir = Path(legacy_d) / "module_dir_fp_cache"  # never created

            with patch.object(app_config, "get_fantasypros_cache_dir", return_value=new_dir):
                with patch.object(fp, "LEGACY_CACHE_DIR", legacy_dir):
                    self.assertIsNone(fp._read_cache("players"))
                    usage = fp._load_usage()
                    self.assertEqual(usage["requests_made_today"], 0)

                    self.assertFalse(legacy_dir.exists())

                    # First legitimate cache write creates ONLY the new dir.
                    fp._atomic_write_json(fp._cache_write_path("players"), {"record_count": 0})

            self.assertTrue(new_dir.exists())
            self.assertFalse(legacy_dir.exists())


if __name__ == "__main__":
    unittest.main()
