"""Focused, fully offline tests for the performance harness credential resolver.

The performance harness must use the same project-owned ESPN resolver as the
production code: canonical ESPN environment variables first, then app-home
credentials.json. The retired ~/.orcha key/value fallback must not return.

All credential-looking values in this file are synthetic fixtures only.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import performance_regression as perf_reg

FAKE_S2 = "TEST_SECRET_DO_NOT_PRINT_S2"
FAKE_SWID = "TEST_SECRET_DO_NOT_PRINT_SWID"


class TestPerfRegressionCredentialResolver(unittest.TestCase):
    def setUp(self):
        self._env_patcher = patch.dict(os.environ, {}, clear=True)
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    @staticmethod
    def _write_app_home_credentials(app_home: Path, *, s2: str, swid: str) -> None:
        app_home.mkdir(parents=True, exist_ok=True)
        (app_home / "credentials.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "espn": {
                        "espn_s2": s2,
                        "swid": swid,
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_environment_pair_wins_over_app_home_file(self):
        with TemporaryDirectory() as d:
            app_home = Path(d) / "app-home"
            self._write_app_home_credentials(
                app_home,
                s2="file-s2",
                swid="file-swid",
            )
            with patch.dict(
                os.environ,
                {
                    "FANTASY_FOOTBALL_MCP_HOME": str(app_home),
                    "ESPN_S2": FAKE_S2,
                    "ESPN_SWID": FAKE_SWID,
                },
            ):
                result = perf_reg.load_credentials()

        self.assertEqual(result, (FAKE_S2, FAKE_SWID))

    def test_app_home_credentials_json_fallback(self):
        with TemporaryDirectory() as d:
            app_home = Path(d) / "app-home"
            self._write_app_home_credentials(app_home, s2=FAKE_S2, swid=FAKE_SWID)
            with patch.dict(
                os.environ,
                {"FANTASY_FOOTBALL_MCP_HOME": str(app_home)},
            ):
                result = perf_reg.load_credentials()

        self.assertEqual(result, (FAKE_S2, FAKE_SWID))

    def test_app_home_override_is_portable_path_contract(self):
        with TemporaryDirectory() as d:
            app_home = Path(d) / "nested" / "ffm-home"
            self._write_app_home_credentials(app_home, s2=FAKE_S2, swid=FAKE_SWID)
            with patch.dict(
                os.environ,
                {"FANTASY_FOOTBALL_MCP_HOME": str(app_home)},
            ):
                result = perf_reg.load_credentials()

        self.assertEqual(result, (FAKE_S2, FAKE_SWID))

    def test_retired_orcha_file_is_not_used(self):
        with TemporaryDirectory() as d:
            fake_home = Path(d) / "home"
            old_secret_dir = fake_home / ".orcha" / "secrets"
            old_secret_dir.mkdir(parents=True)
            (old_secret_dir / "espn_credentials.txt").write_text(
                f"espn_s2={FAKE_S2}\nswid={FAKE_SWID}\n",
                encoding="utf-8",
            )
            app_home = Path(d) / "empty-app-home"
            with patch.dict(
                os.environ,
                {"FANTASY_FOOTBALL_MCP_HOME": str(app_home)},
            ):
                with patch("pathlib.Path.home", return_value=fake_home):
                    with self.assertRaises(perf_reg.HarnessError):
                        perf_reg.load_credentials()

    def test_partial_environment_pair_fails_closed(self):
        with patch.dict(os.environ, {"ESPN_S2": FAKE_S2}):
            with self.assertRaises(perf_reg.HarnessError) as ctx:
                perf_reg.load_credentials()

        message = str(ctx.exception)
        self.assertIn("ESPN_S2 and ESPN_SWID", message)
        self.assertNotIn(FAKE_S2, message)
        self.assertNotIn(FAKE_SWID, message)

    def test_missing_credentials_error_never_contains_secret_values(self):
        with TemporaryDirectory() as d:
            app_home = Path(d) / "empty-app-home"
            with patch.dict(
                os.environ,
                {"FANTASY_FOOTBALL_MCP_HOME": str(app_home)},
            ):
                with self.assertRaises(perf_reg.HarnessError) as ctx:
                    perf_reg.load_credentials()

        message = str(ctx.exception)
        self.assertIn("app-home credentials.json", message)
        self.assertNotIn(FAKE_S2, message)
        self.assertNotIn(FAKE_SWID, message)
        self.assertNotIn(".orcha", message)

    def test_no_store_or_write_credentials_function_exists(self):
        self.assertFalse(hasattr(perf_reg, "store_credentials"))
        self.assertFalse(hasattr(perf_reg, "save_credentials"))
        self.assertFalse(hasattr(perf_reg, "write_credentials"))


if __name__ == "__main__":
    unittest.main()
