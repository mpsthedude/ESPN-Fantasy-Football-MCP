"""SportsGameOdds REST client for the fantasy-football MCP project.

The integration is intentionally read-only. It fetches sportsbook market data
and never places, modifies, or cancels wagers.

Authentication is resolved through app_config and sent only in the x-api-key
header so the key is not written into URLs or ordinary request logs.
"""

from __future__ import annotations

import json
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

import app_config


BASE_URL = "https://api.sportsgameodds.com/v2"
DEFAULT_TIMEOUT_SECONDS = 20
TEAM_METADATA_CACHE_TTL_SECONDS = 24 * 60 * 60
TEAM_SEARCH_DEFAULT_LIMIT = 100

DEFAULT_BOOKMAKERS = (
    "draftkings",
    "fanduel",
    "betmgm",
    "caesars",
    "espnbet",
    "bovada",
    "unibet",
    "pointsbet",
    "williamhill",
)

NFL_TEAM_IDS = {
    "ARI": "ARIZONA_CARDINALS_NFL",
    "ATL": "ATLANTA_FALCONS_NFL",
    "BAL": "BALTIMORE_RAVENS_NFL",
    "BUF": "BUFFALO_BILLS_NFL",
    "CAR": "CAROLINA_PANTHERS_NFL",
    "CHI": "CHICAGO_BEARS_NFL",
    "CIN": "CINCINNATI_BENGALS_NFL",
    "CLE": "CLEVELAND_BROWNS_NFL",
    "DAL": "DALLAS_COWBOYS_NFL",
    "DEN": "DENVER_BRONCOS_NFL",
    "DET": "DETROIT_LIONS_NFL",
    "GB": "GREEN_BAY_PACKERS_NFL",
    "HOU": "HOUSTON_TEXANS_NFL",
    "IND": "INDIANAPOLIS_COLTS_NFL",
    "JAX": "JACKSONVILLE_JAGUARS_NFL",
    "JAC": "JACKSONVILLE_JAGUARS_NFL",
    "KC": "KANSAS_CITY_CHIEFS_NFL",
    "LV": "LAS_VEGAS_RAIDERS_NFL",
    "LAC": "LOS_ANGELES_CHARGERS_NFL",
    "LAR": "LOS_ANGELES_RAMS_NFL",
    "MIA": "MIAMI_DOLPHINS_NFL",
    "MIN": "MINNESOTA_VIKINGS_NFL",
    "NE": "NEW_ENGLAND_PATRIOTS_NFL",
    "NO": "NEW_ORLEANS_SAINTS_NFL",
    "NYG": "NEW_YORK_GIANTS_NFL",
    "NYJ": "NEW_YORK_JETS_NFL",
    "PHI": "PHILADELPHIA_EAGLES_NFL",
    "PIT": "PITTSBURGH_STEELERS_NFL",
    "SEA": "SEATTLE_SEAHAWKS_NFL",
    "SF": "SAN_FRANCISCO_49ERS_NFL",
    "TB": "TAMPA_BAY_BUCCANEERS_NFL",
    "TEN": "TENNESSEE_TITANS_NFL",
    "WAS": "WASHINGTON_COMMANDERS_NFL",
    "WSH": "WASHINGTON_COMMANDERS_NFL",
}

NFL_GAME_ODD_IDS = (
    "points-home-game-ml-home",
    "points-away-game-ml-away",
    "points-home-game-sp-home",
    "points-away-game-sp-away",
    "points-all-game-ou-over",
    "points-all-game-ou-under",
)


class SportsGameOddsError(RuntimeError):
    """Base error for SportsGameOdds integration failures."""


class SportsGameOddsNotConfigured(SportsGameOddsError):
    """Raised when no SportsGameOdds API key is configured."""


class SportsGameOddsAPIError(SportsGameOddsError):
    """Raised when SportsGameOdds returns an unsuccessful API response."""


def _csv(values: Optional[Iterable[str] | str]) -> Optional[str]:
    if values is None:
        return None
    if isinstance(values, str):
        items = [part.strip() for part in values.split(",") if part.strip()]
    else:
        items = [str(part).strip() for part in values if str(part).strip()]
    return ",".join(items) if items else None


def normalize_bookmakers(bookmakers: Optional[Iterable[str] | str]) -> tuple[str, ...]:
    if bookmakers is None:
        return DEFAULT_BOOKMAKERS
    if isinstance(bookmakers, str):
        raw = bookmakers.split(",")
    else:
        raw = bookmakers
    cleaned = tuple(dict.fromkeys(str(item).strip().lower() for item in raw if str(item).strip()))
    return cleaned or DEFAULT_BOOKMAKERS


def resolve_nfl_team_id(team: str) -> str:
    value = (team or "").strip()
    if not value:
        raise ValueError("team is required")

    upper = value.upper().replace(".", "")
    if upper in NFL_TEAM_IDS:
        return NFL_TEAM_IDS[upper]
    if upper.endswith("_NFL"):
        return upper.replace(" ", "_")

    token = upper.replace(" ", "_").replace("-", "_")
    for team_id in set(NFL_TEAM_IDS.values()):
        if token == team_id.removesuffix("_NFL") or token in team_id:
            return team_id
    raise ValueError(
        f"Unknown NFL team {team!r}. Use a standard abbreviation such as DEN, KC, PHI, or an SGO teamID."
    )


def american_to_implied_probability(odds: Any) -> Optional[float]:
    if odds is None:
        return None
    try:
        value = int(str(odds).replace("+", "").strip())
    except (TypeError, ValueError):
        return None
    if value == 0:
        return None
    if value > 0:
        return round(100.0 / (value + 100.0), 6)
    return round((-value) / ((-value) + 100.0), 6)


def _name_score(query: str, display: str) -> float:
    q = " ".join(query.lower().split())
    d = " ".join(display.lower().split())
    if q == d:
        return 1.0
    if q in d or d in q:
        return 0.95
    return SequenceMatcher(None, q, d).ratio()


def select_player(players: list[dict[str, Any]], player_name: str) -> dict[str, Any]:
    if not player_name or not player_name.strip():
        raise ValueError("player_name is required")
    candidates: list[tuple[float, dict[str, Any]]] = []
    for player in players:
        if not isinstance(player, dict):
            continue
        names = _as_mapping(player.get("names"))
        display = names.get("display") or player.get("name") or ""
        if display:
            candidates.append((_name_score(player_name, display), player))
    if not candidates:
        raise SportsGameOddsAPIError("SportsGameOdds returned no named players for that team.")
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    score, selected = candidates[0]
    if score < 0.58:
        suggestions = []
        for _, player in candidates[:5]:
            display = _as_mapping(player.get("names")).get("display") or player.get("name")
            if display:
                suggestions.append(display)
        raise ValueError(
            f"Could not confidently match {player_name!r}. Closest team players: {suggestions}"
        )
    return selected


def _team_name_score(query: str, candidate: str) -> float:
    q = " ".join(str(query).lower().replace("_", " ").replace("-", " ").split())
    c = " ".join(str(candidate).lower().replace("_", " ").replace("-", " ").split())
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if len(q) >= 3 and (q in c or c in q):
        return 0.95
    return SequenceMatcher(None, q, c).ratio()


def _as_mapping(value: Any) -> dict[str, Any]:
    """Return provider mappings safely while tolerating drifted nested shapes."""
    return value if isinstance(value, dict) else {}


def _data_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize provider data collections without following pagination implicitly.

    SportsGameOdds list endpoints currently return ``data`` as a list, but older
    or drifted payloads may expose an object keyed by provider IDs. Missing/null
    data represents an empty page. Malformed scalar rows are ignored.
    """
    data = payload.get("data")
    if data is None:
        return []
    if isinstance(data, list):
        iterable = data
    elif isinstance(data, dict):
        iterable = data.values()
    else:
        return []
    return [row for row in iterable if isinstance(row, dict)]


def _team_summary(team: dict[str, Any]) -> dict[str, Any]:
    return {
        "teamID": team.get("teamID"),
        "sportID": team.get("sportID"),
        "leagueID": team.get("leagueID"),
        "names": _as_mapping(team.get("names")),
        "standings": team.get("standings"),
    }


def _event_starts_at(event: dict[str, Any]) -> Any:
    return _as_mapping(event.get("status")).get("startsAt")


class SportsGameOddsClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        session: Optional[requests.Session] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        cache_dir: Optional[Path] = None,
        team_cache_ttl_seconds: int = TEAM_METADATA_CACHE_TTL_SECONDS,
    ) -> None:
        resolved = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        if resolved is None:
            config = app_config.resolve_sportsgameodds_api_key()
            resolved = config[0] if config is not None else None
        if not resolved:
            raise SportsGameOddsNotConfigured(
                "SportsGameOdds is not configured. Set SPORTSGAMEODDS_API_KEY or add "
                "credentials.sportsgameodds.api_key to the project credentials file."
            )
        self._api_key = resolved
        self._session = session or requests.Session()
        self._timeout_seconds = timeout_seconds
        self._cache_dir = Path(cache_dir) if cache_dir is not None else app_config.get_sportsgameodds_cache_dir()
        self._team_cache_ttl_seconds = max(0, int(team_cache_ttl_seconds))

    def _team_cache_path(self, league_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() else "_" for ch in league_id.lower()).strip("_")
        return self._cache_dir / f"teams_{safe or 'unknown'}.json"

    def _load_team_cache(self, league_id: str) -> tuple[list[dict[str, Any]], bool]:
        """Load a fresh cached league team index, ignoring corrupt/stale state."""
        if self._team_cache_ttl_seconds <= 0:
            return [], False
        path = self._team_cache_path(league_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError, ValueError):
            return [], False
        if not isinstance(raw, dict):
            return [], False
        cached_at = raw.get("cachedAt")
        teams = raw.get("teams")
        if not isinstance(cached_at, (int, float)) or not isinstance(teams, list):
            return [], False
        age = time.time() - float(cached_at)
        if age < 0 or age > self._team_cache_ttl_seconds:
            return [], False
        clean = [team for team in teams if isinstance(team, dict) and team.get("teamID")]
        return clean, True

    def _store_team_cache(
        self,
        league_id: str,
        existing: list[dict[str, Any]],
        new_teams: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge provider team metadata into the persistent league index."""
        merged: dict[str, dict[str, Any]] = {}
        for team in [*existing, *new_teams]:
            if isinstance(team, dict) and team.get("teamID"):
                merged[str(team["teamID"])] = team
        teams = list(merged.values())
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            path = self._team_cache_path(league_id)
            temp = path.with_name(path.name + ".tmp")
            temp.write_text(
                json.dumps({"version": 1, "cachedAt": time.time(), "teams": teams}, separators=(",", ":")),
                encoding="utf-8",
            )
            temp.replace(path)
        except OSError:
            # Cache persistence is an optimization only; read-only filesystems
            # must never make a valid provider request fail.
            pass
        return teams

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        query = {key: value for key, value in params.items() if value is not None}
        url = f"{BASE_URL}/{path.lstrip('/')}"
        try:
            response = self._session.get(
                url,
                headers={"x-api-key": self._api_key},
                params=query,
                timeout=self._timeout_seconds,
            )
        except requests.RequestException as exc:
            raise SportsGameOddsAPIError(
                f"SportsGameOdds request failed ({exc.__class__.__name__})."
            ) from exc

        if response.status_code == 401:
            raise SportsGameOddsAPIError("SportsGameOdds rejected the API key (401).")
        if response.status_code == 403:
            raise SportsGameOddsAPIError("SportsGameOdds denied access to this resource or plan tier (403).")
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            suffix = f" Retry-After: {retry_after}s." if retry_after else ""
            raise SportsGameOddsAPIError(f"SportsGameOdds rate limit reached (429).{suffix}")
        if not response.ok:
            raise SportsGameOddsAPIError(f"SportsGameOdds returned HTTP {response.status_code}.")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SportsGameOddsAPIError("SportsGameOdds returned invalid JSON.") from exc

        if not isinstance(payload, dict):
            raise SportsGameOddsAPIError("SportsGameOdds returned an unexpected response shape.")
        if payload.get("success") is False:
            error = payload.get("error")
            safe_error = str(error)[:300] if error else "unknown API error"
            raise SportsGameOddsAPIError(f"SportsGameOdds API error: {safe_error}")
        return payload

    def usage(self) -> dict[str, Any]:
        return self._get("account/usage")

    def events(self, **params: Any) -> dict[str, Any]:
        return self._get("events", **params)

    def players(self, **params: Any) -> dict[str, Any]:
        return self._get("players", **params)

    def teams(self, **params: Any) -> dict[str, Any]:
        return self._get("teams", **params)

    def sportsbook_team_search(
        self,
        *,
        team_name: str,
        league: str,
        cursor: Optional[str] = None,
        limit: int = TEAM_SEARCH_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Resolve a human team name using cached metadata, then at most one live page."""
        query = (team_name or "").strip()
        league_id = (league or "").strip().upper().replace(" ", "_")
        if not query:
            raise ValueError("team_name is required")
        if not league_id:
            raise ValueError("league is required")

        def rank(teams: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any]]]:
            scored: list[tuple[float, dict[str, Any]]] = []
            for team in teams:
                if not isinstance(team, dict):
                    continue
                names = _as_mapping(team.get("names"))
                candidates = [
                    team.get("teamID"),
                    names.get("short"),
                    names.get("medium"),
                    names.get("long"),
                ]
                score = max(
                    (_team_name_score(query, value) for value in candidates if value),
                    default=0.0,
                )
                scored.append((score, team))
            scored.sort(key=lambda pair: pair[0], reverse=True)
            return scored

        def confident_match(scored: list[tuple[float, dict[str, Any]]]) -> bool:
            if not scored or scored[0][0] < 0.62:
                return False
            second_score = scored[1][0] if len(scored) > 1 else 0.0
            return scored[0][0] >= 0.94 or scored[0][0] - second_score >= 0.04

        cached_teams, cache_valid = self._load_team_cache(league_id)
        cached_scored = rank(cached_teams)
        if cache_valid and confident_match(cached_scored):
            return {
                "query": query,
                "leagueID": league_id,
                "team": _team_summary(cached_scored[0][1]),
                "bestCandidateScore": round(cached_scored[0][0], 4),
                "suggestions": [_team_summary(team) for _, team in cached_scored[:5]],
                "teamsScanned": len(cached_scored),
                "nextCursor": None,
                "notice": None,
                "cache": {"hit": True, "ttlSeconds": self._team_cache_ttl_seconds},
                "interpretation": "Resolved from the local team metadata cache; no provider request was made.",
            }

        payload = self.teams(
            leagueID=league_id,
            cursor=cursor,
            limit=max(1, min(int(limit), 250)),
        )
        page_teams = _data_rows(payload)
        merged_teams = self._store_team_cache(league_id, cached_teams if cache_valid else [], page_teams)
        scored = rank(merged_teams)
        best_score = scored[0][0] if scored else 0.0
        confident = confident_match(scored)
        match = _team_summary(scored[0][1]) if confident else None
        suggestions = [_team_summary(team) for _, team in scored[:5]]
        return {
            "query": query,
            "leagueID": league_id,
            "team": match,
            "bestCandidateScore": round(best_score, 4) if scored else None,
            "suggestions": suggestions,
            "teamsScanned": len(scored),
            "nextCursor": payload.get("nextCursor"),
            "notice": payload.get("notice"),
            "cache": {"hit": False, "ttlSeconds": self._team_cache_ttl_seconds},
            "interpretation": (
                "A live team page was merged into the local metadata cache. "
                "If team is null and nextCursor is present, continue with cursor=nextCursor unchanged."
            ),
        }

    def sportsbook_slate(
        self,
        *,
        league: Optional[str] = None,
        sport: Optional[str] = None,
        team_id: Optional[str] = None,
        bookmakers: Optional[Iterable[str] | str] = None,
        starts_after: Optional[str] = None,
        starts_before: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return one cursor-paginated page of core game markets."""
        if bool(league) == bool(sport):
            raise ValueError("Provide exactly one of league or sport.")
        books = normalize_bookmakers(bookmakers)
        league_id = league.strip().upper().replace(" ", "_") if league else None
        sport_id = sport.strip().upper().replace(" ", "_") if sport else None
        team_value = team_id.strip() if team_id and team_id.strip() else None
        starts_after_value = starts_after.strip() if starts_after and starts_after.strip() else None
        starts_before_value = starts_before.strip() if starts_before and starts_before.strip() else None
        params: dict[str, Any] = {
            "oddsAvailable": "true",
            "oddID": _csv(NFL_GAME_ODD_IDS),
            "bookmakerID": _csv(books),
            "teamID": team_value,
            "startsAfter": starts_after_value,
            "startsBefore": starts_before_value,
            "cursor": cursor,
            "limit": max(1, min(int(limit), 50)),
        }
        if league_id:
            params["leagueID"] = league_id
        else:
            params["sportID"] = sport_id
        payload = self.events(**params)
        events = []
        for event in _data_rows(payload):
            odds = _as_mapping(event.get("odds"))
            game_odds = {odd_id: odds[odd_id] for odd_id in NFL_GAME_ODD_IDS if odd_id in odds}
            events.append(
                {
                    "eventID": event.get("eventID"),
                    "sportID": event.get("sportID"),
                    "leagueID": event.get("leagueID"),
                    "status": event.get("status"),
                    "startsAt": _event_starts_at(event),
                    "teams": event.get("teams"),
                    "odds": game_odds,
                }
            )
        return {
            "leagueID": league_id,
            "sportID": sport_id,
            "bookmakers": list(books),
            "teamID": team_value,
            "startsAfter": starts_after_value,
            "startsBefore": starts_before_value,
            "events": events,
            "nextCursor": payload.get("nextCursor"),
            "notice": payload.get("notice"),
            "interpretation": "Returns core game moneyline, spread, and total markets when offered by the selected books.",
        }

    def sportsbook_player_props(
        self,
        *,
        player_name: str,
        league: str,
        team_id: str,
        event_id: Optional[str] = None,
        stat_id: Optional[str] = None,
        bookmakers: Optional[Iterable[str] | str] = None,
        include_alt_lines: bool = False,
        limit: int = 4,
    ) -> dict[str, Any]:
        """Return generic player props for a provider leagueID and teamID."""
        league_id = (league or "").strip().upper().replace(" ", "_")
        team_value = (team_id or "").strip()
        event_value = event_id.strip() if event_id and event_id.strip() else None
        if not league_id:
            raise ValueError("league is required")
        if not team_value:
            raise ValueError("team_id is required")
        books = normalize_bookmakers(bookmakers)
        event_payload = self.events(
            eventID=event_value,
            leagueID=league_id,
            oddsAvailable="true",
            teamID=team_value,
            bookmakerID=_csv(books),
            includeAltLines="true" if include_alt_lines else "false",
            limit=max(1, min(int(limit), 8)),
        )

        event_rows = _data_rows(event_payload)
        player_by_id: dict[str, dict[str, Any]] = {}
        for event in event_rows:
            event_players = event.get("players") or {}
            if isinstance(event_players, dict):
                iterable = event_players.items()
            elif isinstance(event_players, list):
                iterable = ((p.get("playerID"), p) for p in event_players if isinstance(p, dict))
            else:
                iterable = ()
            for embedded_id, player in iterable:
                if not isinstance(player, dict):
                    continue
                player_id = player.get("playerID") or embedded_id
                if player_id:
                    normalized = dict(player)
                    normalized.setdefault("playerID", player_id)
                    player_by_id[str(player_id)] = normalized

        if player_by_id:
            player = select_player(list(player_by_id.values()), player_name)
        else:
            roster_payload = self.players(leagueID=league_id, teamID=team_value, limit=100)
            player = select_player(_data_rows(roster_payload), player_name)

        player_id = player.get("playerID")
        if not player_id:
            raise SportsGameOddsAPIError("Matched SportsGameOdds player is missing playerID.")

        normalized_events = []
        for event in event_rows:
            props = []
            for odd in _as_mapping(event.get("odds")).values():
                if not isinstance(odd, dict) or odd.get("statEntityID") != player_id:
                    continue
                if stat_id and str(odd.get("statID", "")).lower() != stat_id.lower():
                    continue
                by_bookmaker = {}
                for bookmaker_id, book in _as_mapping(odd.get("byBookmaker")).items():
                    if not isinstance(book, dict):
                        continue
                    by_bookmaker[bookmaker_id] = {
                        "odds": book.get("odds"),
                        "impliedProbability": american_to_implied_probability(book.get("odds")),
                        "overUnder": book.get("overUnder"),
                        "spread": book.get("spread"),
                        "available": book.get("available"),
                        "lastUpdatedAt": book.get("lastUpdatedAt"),
                    }
                props.append(
                    {
                        "oddID": odd.get("oddID"),
                        "marketName": odd.get("marketName"),
                        "statID": odd.get("statID"),
                        "periodID": odd.get("periodID"),
                        "betTypeID": odd.get("betTypeID"),
                        "sideID": odd.get("sideID"),
                        "fairOdds": odd.get("fairOdds"),
                        "fairOverUnder": odd.get("fairOverUnder"),
                        "bookOdds": odd.get("bookOdds"),
                        "bookOverUnder": odd.get("bookOverUnder"),
                        "byBookmaker": by_bookmaker,
                    }
                )
            if props:
                normalized_events.append(
                    {
                        "eventID": event.get("eventID"),
                        "sportID": event.get("sportID"),
                        "leagueID": event.get("leagueID") or league_id,
                        "startsAt": _event_starts_at(event),
                        "teams": event.get("teams"),
                        "props": props,
                    }
                )
        return {
            "player": {
                "playerID": player_id,
                "name": _as_mapping(player.get("names")).get("display") or player.get("name"),
                "position": player.get("position"),
                "teamID": player.get("teamID") or team_value,
            },
            "leagueID": league_id,
            "requestedStatID": stat_id,
            "bookmakers": list(books),
            "includeAltLines": include_alt_lines,
            "events": normalized_events,
            "notice": event_payload.get("notice"),
        }

    def nfl_slate(
        self,
        *,
        bookmakers: Optional[Iterable[str] | str] = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        generic = self.sportsbook_slate(
            league="NFL",
            bookmakers=bookmakers,
            limit=limit,
        )
        # Preserve the original NFL-specific response contract exactly: the
        # generic sport/league metadata is intentionally not added to each row.
        events = [
            {
                "eventID": event.get("eventID"),
                "status": event.get("status"),
                "startsAt": event.get("startsAt"),
                "teams": event.get("teams"),
                "odds": event.get("odds") or {},
            }
            for event in generic.get("events") or []
        ]
        return {
            "leagueID": "NFL",
            "bookmakers": generic.get("bookmakers") or [],
            "events": events,
            "nextCursor": generic.get("nextCursor"),
            "notice": generic.get("notice"),
        }

    def nfl_player_props(
        self,
        *,
        player_name: str,
        team: str,
        stat_id: Optional[str] = None,
        bookmakers: Optional[Iterable[str] | str] = None,
        include_alt_lines: bool = False,
        limit: int = 4,
    ) -> dict[str, Any]:
        team_id = resolve_nfl_team_id(team)
        generic = self.sportsbook_player_props(
            player_name=player_name,
            league="NFL",
            team_id=team_id,
            stat_id=stat_id,
            bookmakers=bookmakers,
            include_alt_lines=include_alt_lines,
            limit=limit,
        )
        # The historical NFL raw-props contract predates the generic leagueID
        # field. Return a copy so delegation cannot mutate the canonical result.
        legacy = dict(generic)
        legacy.pop("leagueID", None)
        return legacy
