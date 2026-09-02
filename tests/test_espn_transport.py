import json
import unittest
from types import SimpleNamespace

import requests

from espn_transport import (
    ESPNAccessError,
    ESPNResponseError,
    ESPNTransport,
    ESPNTransportConfig,
)


class FakeCookies(dict):
    def update(self, values):
        super().update(values)


class FakeHeaders(dict):
    def update(self, values):
        super().update(values)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.cookies = FakeCookies()
        self.headers = FakeHeaders()

    def get(self, url, *, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ESPNTransportTests(unittest.TestCase):
    def test_private_credentials_stay_in_session_cookies(self):
        session = FakeSession([FakeResponse(payload={"id": 123})])
        transport = ESPNTransport("secret-s2", "{secret-swid}", session=session)

        payload = transport.fetch_league(123, 2026, views=["mSettings", "mTeam"])

        self.assertEqual(payload, {"id": 123})
        self.assertEqual(session.cookies["espn_s2"], "secret-s2")
        self.assertEqual(session.cookies["SWID"], "{secret-swid}")
        call = session.calls[0]
        self.assertEqual(
            call["url"],
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/123",
        )
        self.assertEqual(call["params"], [("view", "mSettings"), ("view", "mTeam")])
        self.assertIsNone(call["headers"])
        self.assertNotIn("secret-s2", call["url"])
        self.assertNotIn("secret-swid", call["url"])

    def test_scoring_period_is_encoded_as_query_parameter(self):
        session = FakeSession([FakeResponse(payload={})])
        transport = ESPNTransport(session=session)
        transport.fetch_league(7, 2026, views=["mMatchup"], scoring_period_id=3)
        self.assertEqual(
            session.calls[0]["params"],
            [("view", "mMatchup"), ("scoringPeriodId", "3")],
        )

    def test_fantasy_filter_is_json_header_not_query_parameter(self):
        session = FakeSession([FakeResponse(payload={})])
        transport = ESPNTransport("secret-s2", "{secret-swid}", session=session)
        fantasy_filter = {"schedule": {"filterMatchupPeriodIds": {"value": [15]}}}
        transport.fetch_league(
            7,
            2026,
            views=["mMatchupScore", "mScoreboard"],
            scoring_period_id=16,
            fantasy_filter=fantasy_filter,
        )
        call = session.calls[0]
        self.assertEqual(json.loads(call["headers"]["x-fantasy-filter"]), fantasy_filter)
        self.assertNotIn("x-fantasy-filter", dict(call["params"]))
        serialized = json.dumps(call)
        self.assertNotIn("secret-s2", serialized)
        self.assertNotIn("secret-swid", serialized)

    def test_season_metadata_uses_game_season_endpoint(self):
        session = FakeSession([FakeResponse(payload={"settings": {"proTeams": []}})])
        transport = ESPNTransport("secret-s2", "{secret-swid}", session=session)
        payload = transport.fetch_season(2026, views=["proTeamSchedules_wl"])
        self.assertEqual(payload, {"settings": {"proTeams": []}})
        call = session.calls[0]
        self.assertEqual(
            call["url"],
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026",
        )
        self.assertEqual(call["params"], [("view", "proTeamSchedules_wl")])
        self.assertIsNone(call["headers"])
        self.assertNotIn("secret-s2", json.dumps(call))
        self.assertNotIn("secret-swid", json.dumps(call))

    def test_access_errors_are_status_only_and_secret_safe(self):
        session = FakeSession([FakeResponse(status_code=403)])
        transport = ESPNTransport("very-secret-s2", "{very-secret-swid}", session=session)
        with self.assertRaises(ESPNAccessError) as caught:
            transport.fetch_league(123, 2026)
        text = str(caught.exception)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertNotIn("very-secret-s2", text)
        self.assertNotIn("very-secret-swid", text)

    def test_network_errors_are_wrapped_without_request_details(self):
        session = FakeSession([requests.RequestException("cookie=secret-s2")])
        transport = ESPNTransport("secret-s2", "{secret-swid}", session=session)
        with self.assertRaisesRegex(Exception, "Unable to reach ESPN") as caught:
            transport.fetch_league(123, 2026)
        self.assertNotIn("secret-s2", str(caught.exception))

    def test_non_json_and_non_mapping_league_payloads_fail_closed(self):
        invalid_json = FakeSession([FakeResponse(json_error=ValueError("bad json"))])
        with self.assertRaises(ESPNResponseError):
            ESPNTransport(session=invalid_json).fetch_league(123, 2026)

        wrong_shape = FakeSession([FakeResponse(payload=[{"id": 123}])])
        with self.assertRaises(ESPNResponseError):
            ESPNTransport(session=wrong_shape).fetch_league(123, 2026)

        wrong_season_shape = FakeSession([FakeResponse(payload=[])])
        with self.assertRaises(ESPNResponseError):
            ESPNTransport(session=wrong_season_shape).fetch_season(2026)

    def test_fan_profile_contract_is_centralized_and_url_encoded(self):
        session = FakeSession([FakeResponse(payload={"preferences": []})])
        transport = ESPNTransport(session=session)
        payload = transport.fetch_fan_profile("{ABC DEF}")
        self.assertEqual(payload, {"preferences": []})
        call = session.calls[0]
        self.assertTrue(call["url"].endswith("/%7BABC%20DEF%7D"))
        self.assertEqual(call["params"]["context"], "fantasy")
        self.assertEqual(call["params"]["useCookieAuth"], "true")

    def test_constructor_and_argument_validation_fail_closed(self):
        with self.assertRaises(ValueError):
            ESPNTransport("only-s2", None)
        with self.assertRaises(ValueError):
            ESPNTransportConfig(timeout_seconds=0)

        transport = ESPNTransport(session=FakeSession([]))
        for league_id in (0, -1, True):
            with self.subTest(league_id=league_id):
                with self.assertRaises(ValueError):
                    transport.fetch_league(league_id, 2026)
        with self.assertRaises(ValueError):
            transport.fetch_league(1, 1999)
        with self.assertRaises(ValueError):
            transport.fetch_season(1999)
        with self.assertRaises(ValueError):
            transport.fetch_league(1, 2026, views=[""])
        with self.assertRaises(ValueError):
            transport.fetch_league(1, 2026, fantasy_filter=[])
        with self.assertRaises(ValueError):
            transport.fetch_league(1, 2026, fantasy_filter={"bad": {1, 2}})


if __name__ == "__main__":
    unittest.main()
