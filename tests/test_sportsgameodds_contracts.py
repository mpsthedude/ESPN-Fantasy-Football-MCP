import unittest
from typing import Any

import requests

from sportsgameodds_client import SportsGameOddsAPIError, SportsGameOddsClient


class FakeResponse:
    def __init__(
        self,
        payload: Any = None,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self._json_error = json_error

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class QueueSession:
    def __init__(self, *items: Any) -> None:
        self.items = list(items)
        self.calls: list[dict[str, Any]] = []

    def get(self, url, *, headers, params, timeout):
        self.calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        if not self.items:
            raise AssertionError("Unexpected extra SportsGameOdds provider request")
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class SportsGameOddsContractTests(unittest.TestCase):
    def client(self, *items: Any) -> tuple[SportsGameOddsClient, QueueSession]:
        session = QueueSession(*items)
        return SportsGameOddsClient(api_key="synthetic-key", session=session), session

    def test_http_failure_contracts_are_safe_and_specific(self):
        cases = [
            (401, {}, "rejected the API key (401)"),
            (403, {}, "denied access to this resource or plan tier (403)"),
            (429, {"Retry-After": "17"}, "rate limit reached (429). Retry-After: 17s"),
            (503, {}, "returned HTTP 503"),
        ]
        for status, headers, expected in cases:
            with self.subTest(status=status):
                client, _ = self.client(FakeResponse({}, status_code=status, headers=headers))
                with self.assertRaises(SportsGameOddsAPIError) as caught:
                    client.events(leagueID="NFL")
                self.assertIn(expected, str(caught.exception))

    def test_network_and_invalid_json_failures_do_not_echo_provider_details(self):
        client, _ = self.client(requests.Timeout("secret provider detail that must not leak"))
        with self.assertRaises(SportsGameOddsAPIError) as caught:
            client.events(leagueID="NFL")
        self.assertIn("Timeout", str(caught.exception))
        self.assertNotIn("secret provider detail", str(caught.exception))

        client, _ = self.client(FakeResponse(json_error=ValueError("broken body")))
        with self.assertRaisesRegex(SportsGameOddsAPIError, "returned invalid JSON"):
            client.events(leagueID="NFL")

    def test_api_error_and_top_level_shape_contracts(self):
        client, _ = self.client(FakeResponse({"success": False, "error": "quota unavailable"}))
        with self.assertRaisesRegex(SportsGameOddsAPIError, "quota unavailable"):
            client.events(leagueID="NFL")

        client, _ = self.client(FakeResponse([{"eventID": "unexpected-top-level-list"}]))
        with self.assertRaisesRegex(SportsGameOddsAPIError, "unexpected response shape"):
            client.events(leagueID="NFL")

    def test_slate_accepts_empty_or_missing_data_without_paging(self):
        for payload in ({"success": True}, {"success": True, "data": []}):
            with self.subTest(payload=payload):
                client, session = self.client(FakeResponse(payload))
                result = client.sportsbook_slate(league="NFL", bookmakers=("draftkings",))
                self.assertEqual(result["events"], [])
                self.assertEqual(len(session.calls), 1)

    def test_slate_tolerates_malformed_nested_rows_and_extra_fields(self):
        client, session = self.client(
            FakeResponse(
                {
                    "success": True,
                    "data": [
                        "junk-row",
                        {
                            "eventID": "GAME-1",
                            "leagueID": "NFL",
                            "sportID": "FOOTBALL",
                            "status": "unexpected-status-shape",
                            "teams": {"home": {"teamID": "DENVER_BRONCOS_NFL"}},
                            "odds": ["unexpected-odds-shape"],
                            "newProviderField": {"future": True},
                        },
                    ],
                    "nextCursor": "NEXT-PAGE",
                    "futureTopLevelField": True,
                }
            )
        )
        result = client.sportsbook_slate(league="NFL", bookmakers=("draftkings",))
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(result["nextCursor"], "NEXT-PAGE")
        self.assertEqual(result["events"][0]["eventID"], "GAME-1")
        self.assertIsNone(result["events"][0]["startsAt"])
        self.assertEqual(result["events"][0]["odds"], {})

    def _props_payload(self, players: Any) -> dict[str, Any]:
        return {
            "success": True,
            "data": [
                {
                    "eventID": "GAME-1",
                    "leagueID": "NFL",
                    "sportID": "FOOTBALL",
                    "status": {"startsAt": "2026-09-10T00:00:00Z"},
                    "players": players,
                    "odds": {
                        "prop-1": {
                            "oddID": "prop-1",
                            "marketName": "Passing Yards Over",
                            "statEntityID": "PLAYER-1",
                            "statID": "passing_yards",
                            "periodID": "game",
                            "betTypeID": "ou",
                            "sideID": "over",
                            "byBookmaker": {
                                "draftkings": {
                                    "odds": "+100",
                                    "overUnder": 249.5,
                                    "available": True,
                                },
                                "malformed-book": "not-a-mapping",
                            },
                        }
                    },
                }
            ],
        }

    def test_player_props_supports_embedded_player_mapping_shape(self):
        payload = self._props_payload(
            {
                "PLAYER-1": {
                    "playerID": "PLAYER-1",
                    "names": {"display": "Bo Nix"},
                    "teamID": "DENVER_BRONCOS_NFL",
                    "position": "QB",
                }
            }
        )
        client, session = self.client(FakeResponse(payload))
        result = client.sportsbook_player_props(
            player_name="Bo Nix",
            league="NFL",
            team_id="DENVER_BRONCOS_NFL",
            bookmakers=("draftkings",),
        )
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(result["player"]["playerID"], "PLAYER-1")
        self.assertEqual(result["events"][0]["props"][0]["byBookmaker"]["draftkings"]["impliedProbability"], 0.5)
        self.assertNotIn("malformed-book", result["events"][0]["props"][0]["byBookmaker"])

    def test_player_props_supports_embedded_player_list_shape(self):
        payload = self._props_payload(
            [
                {
                    "playerID": "PLAYER-1",
                    "names": {"display": "Bo Nix"},
                    "teamID": "DENVER_BRONCOS_NFL",
                    "position": "QB",
                }
            ]
        )
        client, session = self.client(FakeResponse(payload))
        result = client.sportsbook_player_props(
            player_name="Bo Nix",
            league="NFL",
            team_id="DENVER_BRONCOS_NFL",
            bookmakers=("draftkings",),
        )
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(result["player"]["name"], "Bo Nix")

    def test_player_props_roster_fallback_is_bounded_to_two_requests(self):
        event_payload = {"success": True, "data": [{"eventID": "GAME-1", "players": None, "odds": {}}]}
        roster_payload = {
            "success": True,
            "data": [
                {
                    "playerID": "PLAYER-1",
                    "names": {"display": "Bo Nix"},
                    "teamID": "DENVER_BRONCOS_NFL",
                }
            ],
            "nextCursor": "IGNORED-ROSTER-CURSOR",
        }
        client, session = self.client(FakeResponse(event_payload), FakeResponse(roster_payload))
        result = client.sportsbook_player_props(
            player_name="Bo Nix",
            league="NFL",
            team_id="DENVER_BRONCOS_NFL",
            bookmakers=("draftkings",),
        )
        self.assertEqual(result["player"]["playerID"], "PLAYER-1")
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(session.calls[0]["url"].endswith("/events"))
        self.assertTrue(session.calls[1]["url"].endswith("/players"))

    def test_slate_does_not_auto_follow_next_cursor(self):
        client, session = self.client(
            FakeResponse({"success": True, "data": [], "nextCursor": "EXPLICIT-NEXT"}),
            FakeResponse({"success": True, "data": [{"eventID": "SHOULD-NOT-BE-FETCHED"}]}),
        )
        result = client.sportsbook_slate(league="NFL", bookmakers=("draftkings",))
        self.assertEqual(result["nextCursor"], "EXPLICIT-NEXT")
        self.assertEqual(len(session.calls), 1)

    def test_team_search_miss_is_bounded_to_one_live_page(self):
        client, session = self.client(
            FakeResponse(
                {
                    "success": True,
                    "data": [
                        {
                            "teamID": "DUKE_BLUE_DEVILS_NCAAB",
                            "sportID": "BASKETBALL",
                            "leagueID": "NCAAB",
                            "names": {"long": "Duke Blue Devils"},
                        }
                    ],
                    "nextCursor": "EXPLICIT-NEXT",
                }
            ),
            FakeResponse({"success": True, "data": []}),
        )
        client._team_cache_ttl_seconds = 0
        result = client.sportsbook_team_search(team_name="Gonzaga Bulldogs", league="NCAAB")
        self.assertIsNone(result["team"])
        self.assertEqual(result["nextCursor"], "EXPLICIT-NEXT")
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
