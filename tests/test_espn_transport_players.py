import json
import unittest

from espn_transport import ESPNResponseError, ESPNTransport


class FakeCookies(dict):
    def update(self, values):
        super().update(values)


class FakeHeaders(dict):
    def update(self, values):
        super().update(values)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.cookies = FakeCookies()
        self.headers = FakeHeaders()

    def get(self, url, *, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return FakeResponse(self.payload)


class ESPNTransportPlayersTests(unittest.TestCase):
    def test_fetch_players_matches_espn_api_active_player_request_contract(self):
        session = FakeSession([{"id": 1, "fullName": "Player One"}])
        transport = ESPNTransport(session=session)
        result = transport.fetch_players(
            2026,
            views=["players_wl"],
            fantasy_filter={"filterActive": {"value": True}},
        )

        self.assertEqual(result[0]["fullName"], "Player One")
        call = session.calls[0]
        self.assertTrue(call["url"].endswith("/seasons/2026/players"))
        self.assertEqual(call["params"], [("view", "players_wl")])
        self.assertEqual(
            json.loads(call["headers"]["x-fantasy-filter"]),
            {"filterActive": {"value": True}},
        )

    def test_fetch_players_keeps_credentials_cookie_only(self):
        session = FakeSession([])
        transport = ESPNTransport("synthetic-s2", "{synthetic-swid}", session=session)
        transport.fetch_players(2026, views=["players_wl"])
        call = session.calls[0]
        self.assertEqual(session.cookies["espn_s2"], "synthetic-s2")
        self.assertEqual(session.cookies["SWID"], "{synthetic-swid}")
        self.assertNotIn("synthetic-s2", call["url"])
        self.assertNotIn("synthetic-swid", call["url"])

    def test_fetch_players_rejects_non_list_success_payload(self):
        transport = ESPNTransport(session=FakeSession({"players": []}))
        with self.assertRaises(ESPNResponseError):
            transport.fetch_players(2026, views=["players_wl"])


if __name__ == "__main__":
    unittest.main()
