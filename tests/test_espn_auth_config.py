"""Deterministic, fully offline tests for the D3C ESPN portable credential
auto-load feature (one-shot lazy bootstrap wired into
ESPNSessionManager.get_transport()/prime()).

Standard library only (unittest + unittest.mock). No network calls: the
project-owned ESPN transport is constructed only from synthetic credentials;
no network requests are issued. The private bootstrap method and transport
wiring are exercised directly. No real credentials anywhere in this file - all
secret-looking values are synthetic test fixtures only.

Each test constructs a FRESH ESPNFantasyFootballAPI() instance rather than
touching the module-level singleton `srv.api`, giving complete isolation
of the one-shot autoload flag/credentials dict without needing any public
reset tool or global monkeypatching between tests.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_config
import espn_fantasy_server as srv

SESSION_ID = srv.SESSION_ID
FAKE_S2 = "TEST_SECRET_DO_NOT_PRINT_S2"
FAKE_SWID = "TEST_SECRET_DO_NOT_PRINT_SWID"


class TestExplicitAuthenticateWins(unittest.TestCase):
    def test_explicit_credentials_not_overwritten_by_autoload(self):
        api = srv.ESPNFantasyFootballAPI()
        api.store_credentials(SESSION_ID, "explicit_s2", "explicit_swid")

        with patch.object(app_config, "resolve_espn_credentials",
                           return_value=(FAKE_S2, FAKE_SWID, "environment")) as mock_resolve:
            api.prime(SESSION_ID)
            mock_resolve.assert_not_called()

        self.assertEqual(api.credentials[SESSION_ID]["espn_s2"], "explicit_s2")
        self.assertEqual(api.credentials[SESSION_ID]["swid"], "explicit_swid")


class TestEnvironmentAutoLoad(unittest.TestCase):
    def test_first_access_loads_env_pair(self):
        api = srv.ESPNFantasyFootballAPI()
        self.assertNotIn(SESSION_ID, api.credentials)

        with patch.object(app_config, "resolve_espn_credentials",
                           return_value=(FAKE_S2, FAKE_SWID, "environment")):
            api.prime(SESSION_ID)

        self.assertIn(SESSION_ID, api.credentials)
        self.assertEqual(api.credentials[SESSION_ID]["espn_s2"], FAKE_S2)
        self.assertEqual(api.credentials[SESSION_ID]["swid"], FAKE_SWID)

    def test_secret_values_never_appear_in_log_diagnostics(self):
        api = srv.ESPNFantasyFootballAPI()
        logged = []
        with patch.object(srv, "log_error", side_effect=lambda msg: logged.append(msg)):
            with patch.object(app_config, "resolve_espn_credentials",
                               return_value=(FAKE_S2, FAKE_SWID, "environment")):
                api.prime(SESSION_ID)
        combined = " ".join(logged)
        self.assertNotIn(FAKE_S2, combined)
        self.assertNotIn(FAKE_SWID, combined)


class TestFileAutoLoad(unittest.TestCase):
    def test_first_access_loads_file_pair(self):
        api = srv.ESPNFantasyFootballAPI()
        with patch.object(app_config, "resolve_espn_credentials",
                           return_value=(FAKE_S2, FAKE_SWID, "project_credentials_file")):
            api.prime(SESSION_ID)
        self.assertEqual(api.credentials[SESSION_ID]["espn_s2"], FAKE_S2)
        self.assertEqual(api.credentials[SESSION_ID]["swid"], FAKE_SWID)


class TestNoConfiguration(unittest.TestCase):
    def test_no_config_leaves_session_unauthenticated(self):
        api = srv.ESPNFantasyFootballAPI()
        with patch.object(app_config, "resolve_espn_credentials", return_value=None):
            api.prime(SESSION_ID)
        self.assertNotIn(SESSION_ID, api.credentials)

    def test_no_config_transport_has_no_auth_cookies(self):
        api = srv.ESPNFantasyFootballAPI()
        with patch.object(app_config, "resolve_espn_credentials", return_value=None):
            transport = api.get_transport(SESSION_ID)

        self.assertIsNone(transport.session.cookies.get("espn_s2"))
        self.assertIsNone(transport.session.cookies.get("SWID"))


class TestAutoLoadOnlyOnce(unittest.TestCase):
    def test_second_call_does_not_reresolve_or_replace(self):
        api = srv.ESPNFantasyFootballAPI()
        with patch.object(app_config, "resolve_espn_credentials",
                           return_value=(FAKE_S2, FAKE_SWID, "environment")) as mock_resolve:
            api.prime(SESSION_ID)
            self.assertEqual(mock_resolve.call_count, 1)

            # Change what a future resolution WOULD return - must never be
            # observed, since credentials are now already in-memory.
            mock_resolve.return_value = ("different_s2", "different_swid", "environment")
            api.prime(SESSION_ID)
            self.assertEqual(mock_resolve.call_count, 1)

        self.assertEqual(api.credentials[SESSION_ID]["espn_s2"], FAKE_S2)
        self.assertEqual(api.credentials[SESSION_ID]["swid"], FAKE_SWID)

    def test_one_shot_flag_prevents_reresolution_even_after_clear(self):
        # This proves the one-shot flag itself (not just "credentials
        # already present") is what prevents re-resolution once an attempt
        # has been made and found nothing - covered separately from the
        # release-critical logout test below, which exercises the exact
        # real clear_credentials() path.
        api = srv.ESPNFantasyFootballAPI()
        with patch.object(app_config, "resolve_espn_credentials", return_value=None) as mock_resolve:
            api.prime(SESSION_ID)
            self.assertEqual(mock_resolve.call_count, 1)
            api.prime(SESSION_ID)
            self.assertEqual(mock_resolve.call_count, 1)


class TestLogoutDoesNotAutoLoginAgain(unittest.TestCase):
    """Release-critical: configured auto-login -> logout() -> next access
    must NOT silently re-authenticate."""

    def test_logout_then_next_access_stays_unauthenticated(self):
        api = srv.ESPNFantasyFootballAPI()

        with patch.object(app_config, "resolve_espn_credentials",
                           return_value=(FAKE_S2, FAKE_SWID, "environment")) as mock_resolve:
            # First ESPN access -> auto-load.
            api.prime(SESSION_ID)
            self.assertIn(SESSION_ID, api.credentials)
            self.assertEqual(mock_resolve.call_count, 1)

            # logout() -> clear_credentials(). Exact real method, untouched
            # by D3C.
            api.clear_credentials(SESSION_ID)
            self.assertNotIn(SESSION_ID, api.credentials)

            # Next ESPN access.
            api.prime(SESSION_ID)

            # Must remain unauthenticated: resolver NOT called again, and
            # no credentials were restored.
            self.assertEqual(mock_resolve.call_count, 1)
            self.assertNotIn(SESSION_ID, api.credentials)


class TestExplicitAuthenticateAfterLogoutWorks(unittest.TestCase):
    def test_authenticate_after_logout_activates_new_pair(self):
        api = srv.ESPNFantasyFootballAPI()

        with patch.object(app_config, "resolve_espn_credentials",
                           return_value=(FAKE_S2, FAKE_SWID, "environment")):
            api.prime(SESSION_ID)
        api.clear_credentials(SESSION_ID)

        # Explicit authenticate() (store_credentials is exactly what the
        # authenticate MCP tool calls) with a brand new pair.
        api.store_credentials(SESSION_ID, "new_explicit_s2", "new_explicit_swid")

        self.assertEqual(api.credentials[SESSION_ID]["espn_s2"], "new_explicit_s2")
        self.assertEqual(api.credentials[SESSION_ID]["swid"], "new_explicit_swid")

        # A further autoload attempt must never touch the explicit pair.
        with patch.object(app_config, "resolve_espn_credentials",
                           return_value=("should_never_be_used_s2", "should_never_be_used_swid",
                                         "environment")) as mock_resolve:
            api.prime(SESSION_ID)
            mock_resolve.assert_not_called()
        self.assertEqual(api.credentials[SESSION_ID]["espn_s2"], "new_explicit_s2")


class TestConfigErrorSafety(unittest.TestCase):
    def test_malformed_config_raises_config_error_not_generic_exception(self):
        api = srv.ESPNFantasyFootballAPI()
        with patch.object(app_config, "resolve_espn_credentials",
                           side_effect=app_config.ConfigError(
                               "ESPN credentials configuration must provide both espn_s2 and swid.")):
            with self.assertRaises(app_config.ConfigError):
                api.prime(SESSION_ID)

    def test_config_error_text_never_contains_fake_secret(self):
        api = srv.ESPNFantasyFootballAPI()
        with patch.object(app_config, "resolve_espn_credentials",
                           side_effect=app_config.ConfigError(
                               "ESPN credentials configuration must provide both espn_s2 and swid.")):
            try:
                api.prime(SESSION_ID)
                self.fail("expected ConfigError")
            except app_config.ConfigError as e:
                self.assertNotIn(FAKE_S2, str(e))
                self.assertNotIn(FAKE_SWID, str(e))
                self.assertNotIn(FAKE_S2, repr(e))
                self.assertNotIn(FAKE_SWID, repr(e))

    def test_config_error_still_marks_one_shot_attempted_no_retry_storm(self):
        api = srv.ESPNFantasyFootballAPI()
        with patch.object(app_config, "resolve_espn_credentials",
                           side_effect=app_config.ConfigError("bad config")) as mock_resolve:
            with self.assertRaises(app_config.ConfigError):
                api.prime(SESSION_ID)
            self.assertEqual(mock_resolve.call_count, 1)
            # Second call: flag already marked True before the first
            # resolver call, so this must return quietly, NOT re-raise or
            # re-invoke the resolver.
            api.prime(SESSION_ID)
            self.assertEqual(mock_resolve.call_count, 1)
        self.assertNotIn(SESSION_ID, api.credentials)


class TestGetTransportEndToEndWiring(unittest.TestCase):
    """Integration-style proof that auto-loaded credentials reach ESPNTransport."""

    def test_get_transport_uses_autoloaded_credentials(self):
        api = srv.ESPNFantasyFootballAPI()

        with patch.object(app_config, "resolve_espn_credentials",
                           return_value=(FAKE_S2, FAKE_SWID, "environment")):
            transport = api.get_transport(SESSION_ID)

        self.assertEqual(transport.session.cookies.get("espn_s2"), FAKE_S2)
        self.assertEqual(transport.session.cookies.get("SWID"), FAKE_SWID)


if __name__ == "__main__":
    unittest.main()
