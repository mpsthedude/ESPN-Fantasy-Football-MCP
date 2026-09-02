"""Deterministic, fully offline tests for app_config.py and its D3B
integration into fantasypros_client.py's credential resolver.

Standard library only (unittest + tempfile + unittest.mock + pathlib).
Never touches the real user home directory or the real Windows registry.
No network calls. No real credentials anywhere in this file - all secret
-looking values below are synthetic test fixtures only.
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
import fantasypros_client as fp_client


FAKE_SECRET = "TEST_SECRET_DO_NOT_PRINT"


class TestGetAppHomeDefault(unittest.TestCase):
    def test_default_is_home_slash_dotdir(self):
        fake_home = Path("/fake/home/dir")
        with patch.object(app_config.Path, "home", return_value=fake_home):
            with patch.dict(os.environ, {}, clear=True):
                result = app_config.get_app_home()
        self.assertEqual(result, fake_home / ".fantasy-football-mcp")


class TestGetAppHomeEnvironmentOverride(unittest.TestCase):
    def test_absolute_path_override_wins(self):
        with patch.dict(os.environ, {"FANTASY_FOOTBALL_MCP_HOME": "/custom/ffm/home"}):
            result = app_config.get_app_home()
        self.assertEqual(result, Path("/custom/ffm/home"))

    def test_user_marker_expanded(self):
        fake_home = Path("/fake/home/dir")
        with patch.object(app_config.Path, "home", return_value=fake_home):
            with patch.dict(os.environ, {"FANTASY_FOOTBALL_MCP_HOME": "~/custom-ffm"}):
                with patch("app_config.os.path.expanduser",
                           side_effect=lambda p: p.replace("~", str(fake_home))):
                    result = app_config.get_app_home()
        self.assertEqual(result, fake_home / "custom-ffm")

    def test_surrounding_whitespace_stripped(self):
        with patch.dict(os.environ, {"FANTASY_FOOTBALL_MCP_HOME": "  /custom/ffm/home  "}):
            result = app_config.get_app_home()
        self.assertEqual(result, Path("/custom/ffm/home"))

    def test_interior_whitespace_preserved(self):
        with patch.dict(os.environ, {"FANTASY_FOOTBALL_MCP_HOME": "/custom/ffm home/x"}):
            result = app_config.get_app_home()
        self.assertEqual(result, Path("/custom/ffm home/x"))


class TestGetAppHomeBlankOverride(unittest.TestCase):
    def test_empty_string_falls_back_to_default(self):
        fake_home = Path("/fake/home/dir")
        with patch.object(app_config.Path, "home", return_value=fake_home):
            with patch.dict(os.environ, {"FANTASY_FOOTBALL_MCP_HOME": ""}):
                result = app_config.get_app_home()
        self.assertEqual(result, fake_home / ".fantasy-football-mcp")

    def test_whitespace_only_falls_back_to_default(self):
        fake_home = Path("/fake/home/dir")
        with patch.object(app_config.Path, "home", return_value=fake_home):
            with patch.dict(os.environ, {"FANTASY_FOOTBALL_MCP_HOME": "   "}):
                result = app_config.get_app_home()
        self.assertEqual(result, fake_home / ".fantasy-football-mcp")

    def test_unset_falls_back_to_default(self):
        fake_home = Path("/fake/home/dir")
        with patch.object(app_config.Path, "home", return_value=fake_home):
            with patch.dict(os.environ, {}, clear=True):
                result = app_config.get_app_home()
        self.assertEqual(result, fake_home / ".fantasy-football-mcp")


class TestGetSportsGameOddsCacheDir(unittest.TestCase):
    def test_sgo_cache_dir_is_app_home_subdir(self):
        fake_home = Path("/fake/home/dir")
        with patch.object(app_config.Path, "home", return_value=fake_home):
            with patch.dict(os.environ, {}, clear=True):
                result = app_config.get_sportsgameodds_cache_dir()
        self.assertEqual(result, fake_home / ".fantasy-football-mcp" / "sgo_cache")

    def test_sgo_cache_dir_respects_app_home_override(self):
        with patch.dict(os.environ, {"FANTASY_FOOTBALL_MCP_HOME": "/custom/ffm"}):
            result = app_config.get_sportsgameodds_cache_dir()
        self.assertEqual(result, Path("/custom/ffm/sgo_cache"))


class TestGetCredentialsPath(unittest.TestCase):
    def test_credentials_path_is_app_home_slash_filename(self):
        fake_home = Path("/fake/home/dir")
        with patch.object(app_config.Path, "home", return_value=fake_home):
            with patch.dict(os.environ, {}, clear=True):
                expected = app_config.get_app_home() / "credentials.json"
                result = app_config.get_credentials_path()
        self.assertEqual(result, expected)


class TestLoadCredentials(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "does_not_exist.json"
            self.assertEqual(app_config.load_credentials(path), {})

    def test_valid_object_loads_normally(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "credentials.json"
            path.write_text(json.dumps({"version": 1, "fantasypros": {"api_key": FAKE_SECRET}}),
                             encoding="utf-8")
            result = app_config.load_credentials(path)
        self.assertEqual(result, {"version": 1, "fantasypros": {"api_key": FAKE_SECRET}})

    def test_empty_file_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "credentials.json"
            path.write_text("", encoding="utf-8")
            with self.assertRaises(app_config.ConfigError):
                app_config.load_credentials(path)

    def test_whitespace_only_file_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "credentials.json"
            path.write_text("   \n  ", encoding="utf-8")
            with self.assertRaises(app_config.ConfigError):
                app_config.load_credentials(path)

    def test_malformed_json_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "credentials.json"
            path.write_text('{"fantasypros": {"api_key": "' + FAKE_SECRET + '"' , encoding="utf-8")
            with self.assertRaises(app_config.ConfigError):
                app_config.load_credentials(path)

    def test_malformed_json_error_does_not_leak_secret_value(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "credentials.json"
            path.write_text('{"fantasypros": {"api_key": "' + FAKE_SECRET + '"', encoding="utf-8")
            try:
                app_config.load_credentials(path)
                self.fail("expected ConfigError")
            except app_config.ConfigError as e:
                self.assertNotIn(FAKE_SECRET, str(e))
                self.assertNotIn(FAKE_SECRET, repr(e))

    def test_list_root_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "credentials.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(app_config.ConfigError):
                app_config.load_credentials(path)

    def test_string_root_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "credentials.json"
            path.write_text('"just a string"', encoding="utf-8")
            with self.assertRaises(app_config.ConfigError):
                app_config.load_credentials(path)

    def test_number_root_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "credentials.json"
            path.write_text("123", encoding="utf-8")
            with self.assertRaises(app_config.ConfigError):
                app_config.load_credentials(path)

    def test_unknown_keys_do_not_fail_parsing(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "credentials.json"
            path.write_text(json.dumps({
                "version": 1,
                "fantasypros": {"api_key": FAKE_SECRET},
                "yahoo": {"client_id": "future", "client_secret": "future"},
                "espn": {"espn_s2": "future", "swid": "future"},
                "some_unknown_future_field": True,
            }), encoding="utf-8")
            result = app_config.load_credentials(path)
        self.assertIn("some_unknown_future_field", result)
        self.assertTrue(result["some_unknown_future_field"])

    def test_partial_provider_object_is_valid(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "credentials.json"
            path.write_text(json.dumps({"version": 1, "fantasypros": {}}), encoding="utf-8")
            result = app_config.load_credentials(path)
        self.assertEqual(result, {"version": 1, "fantasypros": {}})


class TestFantasyProsKeyExtraction(unittest.TestCase):
    def _write(self, d, data):
        path = Path(d) / "credentials.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_valid_non_empty_key(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"fantasypros": {"api_key": FAKE_SECRET}})
            result = app_config.get_fantasypros_api_key_from_credentials(path)
        self.assertEqual(result, FAKE_SECRET)

    def test_blank_key_returns_none(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"fantasypros": {"api_key": ""}})
            result = app_config.get_fantasypros_api_key_from_credentials(path)
        self.assertIsNone(result)

    def test_whitespace_only_key_returns_none(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"fantasypros": {"api_key": "   "}})
            result = app_config.get_fantasypros_api_key_from_credentials(path)
        self.assertIsNone(result)

    def test_missing_fantasypros_section_returns_none(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"version": 1})
            result = app_config.get_fantasypros_api_key_from_credentials(path)
        self.assertIsNone(result)

    def test_missing_api_key_field_returns_none(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"fantasypros": {}})
            result = app_config.get_fantasypros_api_key_from_credentials(path)
        self.assertIsNone(result)

    def test_null_api_key_returns_none(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"fantasypros": {"api_key": None}})
            result = app_config.get_fantasypros_api_key_from_credentials(path)
        self.assertIsNone(result)

    def test_malformed_fantasypros_section_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"fantasypros": "not-an-object"})
            with self.assertRaises(app_config.ConfigError):
                app_config.get_fantasypros_api_key_from_credentials(path)

    def test_malformed_section_error_does_not_leak_key_value(self):
        # Even though this malformed shape has no api_key at all, prove the
        # exception text never echoes back arbitrary file content either.
        with TemporaryDirectory() as d:
            path = self._write(d, {"fantasypros": FAKE_SECRET})
            try:
                app_config.get_fantasypros_api_key_from_credentials(path)
                self.fail("expected ConfigError")
            except app_config.ConfigError as e:
                self.assertNotIn(FAKE_SECRET, str(e))
                self.assertNotIn(FAKE_SECRET, repr(e))


class TestFantasyProsPrecedence(unittest.TestCase):
    """Exercises the real fantasypros_client resolver with every source
    mocked - no real Windows Registry, no real files, no network."""

    def setUp(self):
        # Ensure a clean, deterministic environment for every test in this
        # class regardless of what the real host machine has set.
        self._env_patcher = patch.dict(os.environ, {}, clear=True)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    def test_windows_registry_wins_over_everything(self):
        with patch.object(fp_client, "_read_windows_user_env_registry",
                           return_value="from-registry"):
            with patch.dict(os.environ, {"FANTASYPROS_API_KEY": "from-env"}):
                with patch.object(fp_client.app_config, "get_fantasypros_api_key_from_credentials",
                                   return_value="from-project-file"):
                    with patch.object(fp_client, "_read_secret_file_api_key",
                                       return_value="from-legacy-file"):
                        key, source = fp_client._resolve_fantasypros_api_key_with_source()
        self.assertEqual(key, "from-registry")
        self.assertEqual(source, "windows_user_environment")

    def test_env_wins_when_registry_absent(self):
        with patch.object(fp_client, "_read_windows_user_env_registry", return_value=None):
            with patch.dict(os.environ, {"FANTASYPROS_API_KEY": "from-env"}):
                with patch.object(fp_client.app_config, "get_fantasypros_api_key_from_credentials",
                                   return_value="from-project-file"):
                    with patch.object(fp_client, "_read_secret_file_api_key",
                                       return_value="from-legacy-file"):
                        key, source = fp_client._resolve_fantasypros_api_key_with_source()
        self.assertEqual(key, "from-env")
        self.assertEqual(source, "process_environment")

    def test_project_credentials_file_wins_when_registry_and_env_absent(self):
        with patch.object(fp_client, "_read_windows_user_env_registry", return_value=None):
            with patch.object(fp_client.app_config, "get_fantasypros_api_key_from_credentials",
                               return_value="from-project-file"):
                with patch.object(fp_client, "_read_secret_file_api_key",
                                   return_value="from-legacy-file"):
                    key, source = fp_client._resolve_fantasypros_api_key_with_source()
        self.assertEqual(key, "from-project-file")
        self.assertEqual(source, "project_credentials_file")

    def test_legacy_orcha_file_wins_when_all_prior_sources_absent(self):
        with patch.object(fp_client, "_read_windows_user_env_registry", return_value=None):
            with patch.object(fp_client.app_config, "get_fantasypros_api_key_from_credentials",
                               return_value=None):
                with patch.object(fp_client, "_read_secret_file_api_key",
                                   return_value="from-legacy-file"):
                    key, source = fp_client._resolve_fantasypros_api_key_with_source()
        self.assertEqual(key, "from-legacy-file")
        self.assertEqual(source, "local_secret_file")

    def test_none_returned_when_all_four_sources_absent(self):
        with patch.object(fp_client, "_read_windows_user_env_registry", return_value=None):
            with patch.object(fp_client.app_config, "get_fantasypros_api_key_from_credentials",
                               return_value=None):
                with patch.object(fp_client, "_read_secret_file_api_key", return_value=None):
                    key, source = fp_client._resolve_fantasypros_api_key_with_source()
        self.assertIsNone(key)
        self.assertIsNone(source)

    def test_malformed_project_credentials_file_fails_safely_not_orcha_fallback(self):
        with patch.object(fp_client, "_read_windows_user_env_registry", return_value=None):
            with patch.object(fp_client.app_config, "get_fantasypros_api_key_from_credentials",
                               side_effect=app_config.ConfigError("Invalid credentials configuration JSON at <path>.")):
                with patch.object(fp_client, "_read_secret_file_api_key",
                                   return_value="from-legacy-file") as legacy_mock:
                    with self.assertRaises(app_config.ConfigError):
                        fp_client._resolve_fantasypros_api_key_with_source()
                    legacy_mock.assert_not_called()

    def test_blank_project_key_falls_through_to_legacy_orcha_source(self):
        with patch.object(fp_client, "_read_windows_user_env_registry", return_value=None):
            with patch.object(fp_client.app_config, "get_fantasypros_api_key_from_credentials",
                               return_value="   "):
                with patch.object(fp_client, "_read_secret_file_api_key",
                                   return_value="from-legacy-file"):
                    key, source = fp_client._resolve_fantasypros_api_key_with_source()
        self.assertEqual(key, "from-legacy-file")
        self.assertEqual(source, "local_secret_file")

    def test_existing_source_labels_remain_stable(self):
        # windows_user_environment
        with patch.object(fp_client, "_read_windows_user_env_registry", return_value="v"):
            _, source = fp_client._resolve_fantasypros_api_key_with_source()
        self.assertEqual(source, "windows_user_environment")
        # process_environment
        with patch.object(fp_client, "_read_windows_user_env_registry", return_value=None):
            with patch.dict(os.environ, {"FANTASYPROS_API_KEY": "v"}):
                _, source = fp_client._resolve_fantasypros_api_key_with_source()
        self.assertEqual(source, "process_environment")
        # local_secret_file
        with patch.object(fp_client, "_read_windows_user_env_registry", return_value=None):
            with patch.object(fp_client.app_config, "get_fantasypros_api_key_from_credentials",
                               return_value=None):
                with patch.object(fp_client, "_read_secret_file_api_key", return_value="v"):
                    _, source = fp_client._resolve_fantasypros_api_key_with_source()
        self.assertEqual(source, "local_secret_file")

    def test_new_source_uses_project_credentials_file_label(self):
        with patch.object(fp_client, "_read_windows_user_env_registry", return_value=None):
            with patch.object(fp_client.app_config, "get_fantasypros_api_key_from_credentials",
                               return_value="v"):
                _, source = fp_client._resolve_fantasypros_api_key_with_source()
        self.assertEqual(source, "project_credentials_file")


if __name__ == "__main__":
    unittest.main()


class TestEspnCredentialExtraction(unittest.TestCase):
    def _write(self, d, data):
        path = Path(d) / "credentials.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_missing_espn_section_returns_none(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"version": 1})
            result = app_config.get_espn_credentials_from_credentials(path)
        self.assertIsNone(result)

    def test_empty_espn_object_returns_none(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {}})
            result = app_config.get_espn_credentials_from_credentials(path)
        self.assertIsNone(result)

    def test_valid_pair_returned(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": FAKE_SECRET, "swid": "FAKE_SWID"}})
            result = app_config.get_espn_credentials_from_credentials(path)
        self.assertEqual(result, (FAKE_SECRET, "FAKE_SWID"))

    def test_whitespace_around_values_stripped(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": "  " + FAKE_SECRET + "  ",
                                             "swid": "  FAKE_SWID  "}})
            result = app_config.get_espn_credentials_from_credentials(path)
        self.assertEqual(result, (FAKE_SECRET, "FAKE_SWID"))

    def test_espn_s2_only_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": FAKE_SECRET}})
            with self.assertRaises(app_config.ConfigError):
                app_config.get_espn_credentials_from_credentials(path)

    def test_swid_only_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"swid": "FAKE_SWID"}})
            with self.assertRaises(app_config.ConfigError):
                app_config.get_espn_credentials_from_credentials(path)

    def test_blank_espn_s2_with_valid_swid_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": "   ", "swid": "FAKE_SWID"}})
            with self.assertRaises(app_config.ConfigError):
                app_config.get_espn_credentials_from_credentials(path)

    def test_valid_espn_s2_with_blank_swid_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": FAKE_SECRET, "swid": "   "}})
            with self.assertRaises(app_config.ConfigError):
                app_config.get_espn_credentials_from_credentials(path)

    def test_espn_section_non_object_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": "not-an-object"})
            with self.assertRaises(app_config.ConfigError):
                app_config.get_espn_credentials_from_credentials(path)

    def test_espn_s2_non_string_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": 12345, "swid": "FAKE_SWID"}})
            with self.assertRaises(app_config.ConfigError):
                app_config.get_espn_credentials_from_credentials(path)

    def test_swid_non_string_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": FAKE_SECRET, "swid": True}})
            with self.assertRaises(app_config.ConfigError):
                app_config.get_espn_credentials_from_credentials(path)

    def test_error_strings_do_not_expose_fake_credential_values(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": FAKE_SECRET}})
            try:
                app_config.get_espn_credentials_from_credentials(path)
                self.fail("expected ConfigError")
            except app_config.ConfigError as e:
                self.assertNotIn(FAKE_SECRET, str(e))
                self.assertNotIn(FAKE_SECRET, repr(e))


class TestEspnEnvironmentPrecedence(unittest.TestCase):
    def setUp(self):
        self._env_patcher = patch.dict(os.environ, {}, clear=True)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    def _write(self, d, data):
        path = Path(d) / "credentials.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_both_env_vars_win_over_file(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": "from-file-s2", "swid": "from-file-swid"}})
            with patch.dict(os.environ, {"ESPN_S2": "from-env-s2", "ESPN_SWID": "from-env-swid"}):
                result = app_config.resolve_espn_credentials(path)
        self.assertEqual(result, ("from-env-s2", "from-env-swid", "environment"))

    def test_both_env_vars_absent_falls_back_to_file(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": "from-file-s2", "swid": "from-file-swid"}})
            result = app_config.resolve_espn_credentials(path)
        self.assertEqual(result, ("from-file-s2", "from-file-swid", "project_credentials_file"))

    def test_blank_env_pair_falls_back_to_file(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": "from-file-s2", "swid": "from-file-swid"}})
            with patch.dict(os.environ, {"ESPN_S2": "   ", "ESPN_SWID": "   "}):
                result = app_config.resolve_espn_credentials(path)
        self.assertEqual(result, ("from-file-s2", "from-file-swid", "project_credentials_file"))

    def test_only_espn_s2_env_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": "from-file-s2", "swid": "from-file-swid"}})
            with patch.dict(os.environ, {"ESPN_S2": "from-env-s2"}):
                with self.assertRaises(app_config.ConfigError):
                    app_config.resolve_espn_credentials(path)

    def test_only_espn_swid_env_raises_config_error(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": "from-file-s2", "swid": "from-file-swid"}})
            with patch.dict(os.environ, {"ESPN_SWID": "from-env-swid"}):
                with self.assertRaises(app_config.ConfigError):
                    app_config.resolve_espn_credentials(path)

    def test_partial_env_does_not_combine_with_file(self):
        # Only ESPN_S2 set in env, a DIFFERENT complete pair exists in the
        # file. Must raise, never silently produce a mixed pair.
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": "from-file-s2", "swid": "from-file-swid"}})
            with patch.dict(os.environ, {"ESPN_S2": "from-env-s2"}):
                try:
                    app_config.resolve_espn_credentials(path)
                    self.fail("expected ConfigError")
                except app_config.ConfigError:
                    pass

    def test_no_env_no_file_returns_none(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "credentials.json"  # never created
            result = app_config.resolve_espn_credentials(path)
        self.assertIsNone(result)

    def test_source_label_is_environment_when_env_used(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "credentials.json"
            with patch.dict(os.environ, {"ESPN_S2": "s", "ESPN_SWID": "w"}):
                result = app_config.resolve_espn_credentials(path)
        self.assertEqual(result[2], "environment")

    def test_source_label_is_project_credentials_file_when_file_used(self):
        with TemporaryDirectory() as d:
            path = self._write(d, {"espn": {"espn_s2": "s", "swid": "w"}})
            result = app_config.resolve_espn_credentials(path)
        self.assertEqual(result[2], "project_credentials_file")
