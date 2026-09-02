import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_config
from sportsgameodds_client import (
    SportsGameOddsClient,
    american_to_implied_probability,
    normalize_bookmakers,
    resolve_nfl_team_id,
    select_player,
)


class FakeResponse:
    status_code = 200
    ok = True
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, *, headers, params, timeout):
        self.calls.append(
            {"url": url, "headers": headers, "params": params, "timeout": timeout}
        )
        return FakeResponse(self.payload)


class SportsGameOddsClientTests(unittest.TestCase):
    def test_normalize_bookmakers_defaults_and_dedupes(self):
        self.assertIn("draftkings", normalize_bookmakers(None))
        self.assertEqual(
            normalize_bookmakers("FanDuel, draftkings, fanduel"),
            ("fanduel", "draftkings"),
        )

    def test_resolve_nfl_team_id_from_espn_abbreviation(self):
        self.assertEqual(resolve_nfl_team_id("DEN"), "DENVER_BRONCOS_NFL")
        self.assertEqual(resolve_nfl_team_id("WSH"), "WASHINGTON_COMMANDERS_NFL")

    def test_american_to_implied_probability(self):
        self.assertEqual(american_to_implied_probability("+100"), 0.5)
        self.assertAlmostEqual(
            american_to_implied_probability("-110"),
            0.52381,
            places=5,
        )
        self.assertIsNone(american_to_implied_probability(None))

    def test_select_player_prefers_exact_name(self):
        players = [
            {"playerID": "1", "names": {"display": "Bo Nix"}},
            {"playerID": "2", "names": {"display": "Nick Bonitto"}},
        ]
        self.assertEqual(select_player(players, "Bo Nix")["playerID"], "1")

    def test_client_uses_header_not_query_parameter(self):
        session = FakeSession({"success": True, "data": []})
        client = SportsGameOddsClient(api_key="super-secret", session=session)

        client.events(leagueID="NFL", oddsAvailable="true")

        call = session.calls[0]
        self.assertEqual(call["headers"]["x-api-key"], "super-secret")
        self.assertNotIn("apiKey", call["params"])
        self.assertNotIn("super-secret", call["url"])

    def test_team_metadata_cache_reused_across_client_instances(self):
        payload = {
            "success": True,
            "data": [
                {
                    "sportID": "FOOTBALL",
                    "leagueID": "NFL",
                    "teamID": "DENVER_BRONCOS_NFL",
                    "names": {"short": "DEN", "medium": "Broncos", "long": "Denver Broncos"},
                },
                {
                    "sportID": "FOOTBALL",
                    "leagueID": "NFL",
                    "teamID": "KANSAS_CITY_CHIEFS_NFL",
                    "names": {"short": "KC", "medium": "Chiefs", "long": "Kansas City Chiefs"},
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            first_session = FakeSession(payload)
            first = SportsGameOddsClient(
                api_key="synthetic-key", session=first_session, cache_dir=cache_dir
            )
            first_result = first.sportsbook_team_search(team_name="Broncos", league="NFL")
            self.assertEqual(len(first_session.calls), 1)
            self.assertFalse(first_result["cache"]["hit"])

            second_session = FakeSession({"success": True, "data": []})
            second = SportsGameOddsClient(
                api_key="synthetic-key", session=second_session, cache_dir=cache_dir
            )
            second_result = second.sportsbook_team_search(team_name="Chiefs", league="NFL")
            self.assertEqual(second_session.calls, [])
            self.assertTrue(second_result["cache"]["hit"])
            self.assertEqual(second_result["team"]["teamID"], "KANSAS_CITY_CHIEFS_NFL")

    def test_team_metadata_cache_never_persists_api_key(self):
        secret = "SYNTHETIC_SGO_SECRET_DO_NOT_CACHE"
        payload = {
            "success": True,
            "data": [{
                "sportID": "FOOTBALL",
                "leagueID": "NFL",
                "teamID": "DENVER_BRONCOS_NFL",
                "names": {"short": "DEN", "medium": "Broncos", "long": "Denver Broncos"},
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            client = SportsGameOddsClient(api_key=secret, session=FakeSession(payload), cache_dir=cache_dir)
            client.sportsbook_team_search(team_name="Broncos", league="NFL")
            cached = (cache_dir / "teams_nfl.json").read_text(encoding="utf-8")
            self.assertNotIn(secret, cached)
            self.assertIn("DENVER_BRONCOS_NFL", cached)

    def test_disabled_team_cache_forces_live_request(self):
        payload = {
            "success": True,
            "data": [{
                "sportID": "FOOTBALL",
                "leagueID": "NFL",
                "teamID": "DENVER_BRONCOS_NFL",
                "names": {"short": "DEN", "medium": "Broncos", "long": "Denver Broncos"},
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            SportsGameOddsClient(
                api_key="synthetic-key", session=FakeSession(payload), cache_dir=cache_dir
            ).sportsbook_team_search(team_name="Broncos", league="NFL")

            live_session = FakeSession(payload)
            client = SportsGameOddsClient(
                api_key="synthetic-key",
                session=live_session,
                cache_dir=cache_dir,
                team_cache_ttl_seconds=0,
            )
            result = client.sportsbook_team_search(team_name="Broncos", league="NFL")
            self.assertEqual(len(live_session.calls), 1)
            self.assertFalse(result["cache"]["hit"])

    def test_corrupt_team_cache_falls_back_to_live_request(self):
        payload = {
            "success": True,
            "data": [{
                "sportID": "FOOTBALL",
                "leagueID": "NFL",
                "teamID": "DENVER_BRONCOS_NFL",
                "names": {"short": "DEN", "medium": "Broncos", "long": "Denver Broncos"},
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir)
            (cache_dir / "teams_nfl.json").write_text("{not valid json", encoding="utf-8")
            session = FakeSession(payload)
            client = SportsGameOddsClient(api_key="synthetic-key", session=session, cache_dir=cache_dir)
            result = client.sportsbook_team_search(team_name="Broncos", league="NFL")
            self.assertEqual(len(session.calls), 1)
            self.assertFalse(result["cache"]["hit"])
            self.assertEqual(result["team"]["teamID"], "DENVER_BRONCOS_NFL")

    def test_sportsbook_team_search_resolves_human_name_on_one_page(self):
        session = FakeSession({
            "success": True,
            "data": [
                {
                    "sportID": "FOOTBALL",
                    "leagueID": "NFL",
                    "teamID": "DENVER_BRONCOS_NFL",
                    "names": {"short": "DEN", "medium": "Broncos", "long": "Denver Broncos"},
                },
                {
                    "sportID": "FOOTBALL",
                    "leagueID": "NFL",
                    "teamID": "KANSAS_CITY_CHIEFS_NFL",
                    "names": {"short": "KC", "medium": "Chiefs", "long": "Kansas City Chiefs"},
                },
            ],
        })
        client = SportsGameOddsClient(
            api_key="synthetic-key", session=session, team_cache_ttl_seconds=0
        )

        result = client.sportsbook_team_search(team_name="broncos", league="nfl")

        self.assertTrue(session.calls[0]["url"].endswith("/teams"))
        self.assertEqual(session.calls[0]["params"]["leagueID"], "NFL")
        self.assertEqual(session.calls[0]["params"]["limit"], 100)
        self.assertEqual(result["team"]["teamID"], "DENVER_BRONCOS_NFL")
        self.assertEqual(result["teamsScanned"], 2)

    def test_sportsbook_team_search_returns_cursor_when_page_has_no_confident_match(self):
        session = FakeSession({
            "success": True,
            "data": [{
                "sportID": "BASKETBALL",
                "leagueID": "NCAAB",
                "teamID": "DUKE_BLUE_DEVILS_NCAAB",
                "names": {"short": "DUKE", "medium": "Blue Devils", "long": "Duke Blue Devils"},
            }],
            "nextCursor": "NEXT-TEAM-PAGE",
        })
        client = SportsGameOddsClient(
            api_key="synthetic-key", session=session, team_cache_ttl_seconds=0
        )

        result = client.sportsbook_team_search(
            team_name="Gonzaga Bulldogs", league="NCAAB", cursor="CURRENT-TEAM-PAGE"
        )

        self.assertEqual(session.calls[0]["params"]["cursor"], "CURRENT-TEAM-PAGE")
        self.assertIsNone(result["team"])
        self.assertEqual(result["nextCursor"], "NEXT-TEAM-PAGE")
        self.assertEqual(result["suggestions"][0]["teamID"], "DUKE_BLUE_DEVILS_NCAAB")

    def test_sportsbook_player_props_forwards_exact_event_id(self):
        session = FakeSession({
            "success": True,
            "data": [{
                "eventID": "GAME-123",
                "players": {
                    "PLAYER-1": {
                        "playerID": "PLAYER-1",
                        "names": {"display": "Star Player"},
                        "teamID": "TEAM-1",
                    }
                },
                "odds": {},
            }],
        })
        client = SportsGameOddsClient(api_key="synthetic-key", session=session)

        result = client.sportsbook_player_props(
            player_name="Star Player",
            league="NBA",
            team_id="TEAM-1",
            event_id="  GAME-123  ",
            bookmakers=("draftkings",),
        )

        self.assertEqual(session.calls[0]["params"]["eventID"], "GAME-123")
        self.assertEqual(result["player"]["playerID"], "PLAYER-1")

    def test_sportsbook_player_props_omits_event_id_when_not_supplied(self):
        session = FakeSession({
            "success": True,
            "data": [{
                "eventID": "GAME-123",
                "players": {
                    "PLAYER-1": {
                        "playerID": "PLAYER-1",
                        "names": {"display": "Star Player"},
                        "teamID": "TEAM-1",
                    }
                },
                "odds": {},
            }],
        })
        client = SportsGameOddsClient(api_key="synthetic-key", session=session)

        client.sportsbook_player_props(
            player_name="Star Player",
            league="NBA",
            team_id="TEAM-1",
            bookmakers=("draftkings",),
        )

        self.assertNotIn("eventID", session.calls[0]["params"])

    def test_sportsbook_slate_forwards_cursor_and_returns_next_cursor(self):
        session = FakeSession({"success": True, "data": [], "nextCursor": "NEXT+/opaque=="})
        client = SportsGameOddsClient(api_key="synthetic-key", session=session)
        result = client.sportsbook_slate(
            league="NBA", bookmakers=("draftkings",), cursor="CURRENT+/opaque==", limit=20
        )
        self.assertEqual(session.calls[0]["params"]["cursor"], "CURRENT+/opaque==")
        self.assertEqual(result["nextCursor"], "NEXT+/opaque==")

    def test_sportsbook_slate_forwards_and_echoes_team_id(self):
        session = FakeSession({"success": True, "data": []})
        client = SportsGameOddsClient(api_key="synthetic-key", session=session)

        result = client.sportsbook_slate(
            league="NFL", team_id="  DENVER_BRONCOS_NFL  ", bookmakers=("draftkings",)
        )

        self.assertEqual(session.calls[0]["params"]["teamID"], "DENVER_BRONCOS_NFL")
        self.assertEqual(result["teamID"], "DENVER_BRONCOS_NFL")

    def test_sportsbook_slate_forwards_and_echoes_date_window(self):
        session = FakeSession({"success": True, "data": []})
        client = SportsGameOddsClient(api_key="synthetic-key", session=session)
        result = client.sportsbook_slate(
            league="NCAAF",
            bookmakers=("draftkings",),
            starts_after=" 2026-09-05T00:00:00-04:00 ",
            starts_before="2026-09-06T00:00:00-04:00",
        )
        params = session.calls[0]["params"]
        self.assertEqual(params["startsAfter"], "2026-09-05T00:00:00-04:00")
        self.assertEqual(params["startsBefore"], "2026-09-06T00:00:00-04:00")
        self.assertEqual(result["startsAfter"], "2026-09-05T00:00:00-04:00")
        self.assertEqual(result["startsBefore"], "2026-09-06T00:00:00-04:00")

    def test_sportsbook_slate_first_page_omits_cursor_query_parameter(self):
        session = FakeSession({"success": True, "data": []})
        client = SportsGameOddsClient(api_key="synthetic-key", session=session)
        client.sportsbook_slate(league="NBA", bookmakers=("draftkings",))
        self.assertNotIn("cursor", session.calls[0]["params"])
        self.assertNotIn("teamID", session.calls[0]["params"])
        self.assertNotIn("startsAfter", session.calls[0]["params"])
        self.assertNotIn("startsBefore", session.calls[0]["params"])

    def test_sportsgameodds_config_environment_precedence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = Path(temp_dir) / "credentials.json"
            credentials.write_text(
                json.dumps({"sportsgameodds": {"api_key": "file-key"}}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"SPORTSGAMEODDS_API_KEY": " env-key "}, clear=False):
                self.assertEqual(
                    app_config.resolve_sportsgameodds_api_key(credentials),
                    ("env-key", "environment"),
                )

    def test_sportsgameodds_config_file_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials = Path(temp_dir) / "credentials.json"
            credentials.write_text(
                json.dumps({"sportsgameodds": {"api_key": " file-key "}}),
                encoding="utf-8",
            )
            env = dict(os.environ)
            env.pop("SPORTSGAMEODDS_API_KEY", None)
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(
                    app_config.resolve_sportsgameodds_api_key(credentials),
                    ("file-key", "project_credentials_file"),
                )


if __name__ == "__main__":
    unittest.main()
