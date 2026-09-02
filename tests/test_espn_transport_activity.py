import json
import unittest

from espn_transport import ESPNTransport


class FakeCookies(dict):
    def update(self, values):
        super().update(values)


class FakeHeaders(dict):
    def update(self, values):
        super().update(values)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.cookies = FakeCookies()
        self.headers = FakeHeaders()

    def get(self, url, *, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return FakeResponse(self.payload)


class ESPNActivityTransportTests(unittest.TestCase):
    def test_communication_subresource_keeps_auth_cookie_only(self):
        session = FakeSession({"topics": []})
        transport = ESPNTransport("secret-s2", "{secret-swid}", session=session)
        fantasy_filter = {
            "topics": {
                "filterType": {"value": ["ACTIVITY_TRANSACTIONS"]},
                "limit": 25,
                "offset": 0,
            }
        }

        payload = transport.fetch_league_communication(
            123,
            2026,
            views=["kona_league_communication"],
            fantasy_filter=fantasy_filter,
        )

        self.assertEqual(payload, {"topics": []})
        call = session.calls[0]
        self.assertEqual(
            call["url"],
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/123/communication/",
        )
        self.assertEqual(call["params"], [("view", "kona_league_communication")])
        self.assertEqual(json.loads(call["headers"]["x-fantasy-filter"]), fantasy_filter)
        self.assertEqual(session.cookies["espn_s2"], "secret-s2")
        self.assertEqual(session.cookies["SWID"], "{secret-swid}")
        serialized_request = json.dumps(call)
        self.assertNotIn("secret-s2", serialized_request)
        self.assertNotIn("secret-swid", serialized_request)


if __name__ == "__main__":
    unittest.main()
