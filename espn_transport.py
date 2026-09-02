"""Project-owned HTTP transport for ESPN fantasy read endpoints.

This module intentionally owns request construction, authentication cookies,
timeouts, JSON validation, and secret-safe error boundaries. Higher-level code
should depend on this transport instead of reaching into espn-api request
internals directly.

ESPN does not publish a supported public Fantasy Football API contract for
these endpoints. Treat endpoint/view shapes as integration contracts that must
be covered by tests and changed deliberately when ESPN changes them.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable, Optional
from urllib.parse import quote

import requests


DEFAULT_TIMEOUT_SECONDS = 20.0
FAN_API_BASE = "https://fan.api.espn.com/apis/v2/fans"
FFL_API_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
USER_AGENT = "fantasy-football-mcp/0.2"


class ESPNTransportError(RuntimeError):
    """Base class for secret-safe ESPN transport failures."""


class ESPNAccessError(ESPNTransportError):
    """ESPN rejected or could not resolve a requested resource."""

    def __init__(self, status_code: int):
        self.status_code = int(status_code)
        super().__init__(f"ESPN request failed with HTTP {self.status_code}")


class ESPNResponseError(ESPNTransportError):
    """ESPN returned a successful HTTP response that was not usable JSON."""


@dataclass(frozen=True)
class ESPNTransportConfig:
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    fan_api_base: str = FAN_API_BASE
    ffl_api_base: str = FFL_API_BASE
    user_agent: str = USER_AGENT

    def __post_init__(self) -> None:
        if isinstance(self.timeout_seconds, bool):
            raise ValueError("timeout_seconds must be a positive number")
        try:
            timeout = float(self.timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout_seconds must be a positive number") from exc
        if timeout <= 0:
            raise ValueError("timeout_seconds must be a positive number")
        object.__setattr__(self, "timeout_seconds", timeout)


class ESPNTransport:
    """Thin, testable ESPN HTTP client with credentials kept inside a Session."""

    def __init__(
        self,
        espn_s2: Optional[str] = None,
        swid: Optional[str] = None,
        *,
        session: Optional[requests.Session] = None,
        config: Optional[ESPNTransportConfig] = None,
    ) -> None:
        if bool(espn_s2) != bool(swid):
            raise ValueError("ESPN authentication requires both espn_s2 and swid")

        self.config = config or ESPNTransportConfig()
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": self.config.user_agent,
            }
        )
        if espn_s2 and swid:
            self.session.cookies.update({"espn_s2": espn_s2, "SWID": swid})

    @staticmethod
    def _validate_positive_int(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{label} must be a positive integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a positive integer") from exc
        if parsed <= 0:
            raise ValueError(f"{label} must be a positive integer")
        return parsed

    @staticmethod
    def _validate_year(year: Any) -> int:
        if isinstance(year, bool) or not isinstance(year, int) or not (2000 <= year <= 2100):
            raise ValueError("year must be an integer between 2000 and 2100")
        return year

    @staticmethod
    def _view_params(views: Optional[Iterable[str]]) -> list[tuple[str, str]]:
        if views is None:
            return []
        params: list[tuple[str, str]] = []
        for view in views:
            if not isinstance(view, str) or not view.strip():
                raise ValueError("views must contain non-empty strings")
            params.append(("view", view.strip()))
        return params

    @staticmethod
    def _fantasy_filter_header(fantasy_filter: Optional[dict]) -> Optional[str]:
        if fantasy_filter is None:
            return None
        if not isinstance(fantasy_filter, dict):
            raise ValueError("fantasy_filter must be a dictionary or None")
        try:
            return json.dumps(fantasy_filter, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("fantasy_filter must be JSON serializable") from exc

    def _get_json(self, url: str, *, params: Any = None, headers: Optional[dict] = None) -> Any:
        try:
            response = self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            # Never include the original exception text because third-party
            # request errors can include request details. Credentials stay in
            # cookies and are never serialized into our public error message.
            raise ESPNTransportError("Unable to reach ESPN") from exc

        if response.status_code >= 400:
            raise ESPNAccessError(response.status_code)

        try:
            return response.json()
        except ValueError as exc:
            raise ESPNResponseError("ESPN returned a non-JSON response") from exc

    def fetch_fan_profile(self, swid: str) -> Any:
        """Fetch the authenticated ESPN fan profile used for league discovery."""
        cleaned = (swid or "").strip()
        if not cleaned:
            raise ValueError("swid is required")
        url = f"{self.config.fan_api_base}/{quote(cleaned, safe='')}"
        return self._get_json(
            url,
            params={
                "displayHiddenPrefs": "true",
                "context": "fantasy",
                "useCookieAuth": "true",
                "source": "fantasyweb",
            },
        )

    def fetch_league(
        self,
        league_id: int,
        year: int,
        *,
        views: Optional[Iterable[str]] = None,
        scoring_period_id: Optional[int] = None,
        fantasy_filter: Optional[dict] = None,
    ) -> dict:
        """Fetch a raw ESPN Fantasy Football league payload.

        The URL/view/filter contract is intentionally centralized here so
        migrations away from espn-api do not duplicate endpoint construction.
        """
        league_id = self._validate_positive_int(league_id, "league_id")
        year = self._validate_year(year)
        params = self._view_params(views)
        if scoring_period_id is not None:
            scoring_period_id = self._validate_positive_int(scoring_period_id, "scoring_period_id")
            params.append(("scoringPeriodId", str(scoring_period_id)))

        headers = None
        encoded_filter = self._fantasy_filter_header(fantasy_filter)
        if encoded_filter is not None:
            headers = {"x-fantasy-filter": encoded_filter}

        url = (
            f"{self.config.ffl_api_base}/seasons/{year}/segments/0/leagues/{league_id}"
        )
        payload = self._get_json(url, params=params, headers=headers)
        if not isinstance(payload, dict):
            raise ESPNResponseError("ESPN returned an unexpected league payload")
        return payload

    def fetch_league_communication(
        self,
        league_id: int,
        year: int,
        *,
        views: Optional[Iterable[str]] = None,
        fantasy_filter: Optional[dict] = None,
    ) -> dict:
        """Fetch ESPN's league communication/activity subresource."""
        league_id = self._validate_positive_int(league_id, "league_id")
        year = self._validate_year(year)
        params = self._view_params(views)
        headers = None
        encoded_filter = self._fantasy_filter_header(fantasy_filter)
        if encoded_filter is not None:
            headers = {"x-fantasy-filter": encoded_filter}
        url = (
            f"{self.config.ffl_api_base}/seasons/{year}/segments/0/leagues/{league_id}/communication/"
        )
        payload = self._get_json(url, params=params, headers=headers)
        if not isinstance(payload, dict):
            raise ESPNResponseError("ESPN returned an unexpected communication payload")
        return payload


    def fetch_season(
        self,
        year: int,
        *,
        views: Optional[Iterable[str]] = None,
    ) -> dict:
        """Fetch game/season-level ESPN Fantasy metadata such as pro schedules."""
        year = self._validate_year(year)
        params = self._view_params(views)
        url = f"{self.config.ffl_api_base}/seasons/{year}"
        payload = self._get_json(url, params=params)
        if not isinstance(payload, dict):
            raise ESPNResponseError("ESPN returned an unexpected season payload")
        return payload


    def fetch_players(
        self,
        year: int,
        *,
        views: Optional[Iterable[str]] = None,
        fantasy_filter: Optional[dict] = None,
    ) -> list[dict]:
        """Fetch season-level ESPN Fantasy player rows.

        This centralizes the /seasons/{year}/players contract used by the
        completed-draft migration. Higher-level callers own the specific view
        and filter semantics they need.
        """
        year = self._validate_year(year)
        params = self._view_params(views)
        headers = None
        encoded_filter = self._fantasy_filter_header(fantasy_filter)
        if encoded_filter is not None:
            headers = {"x-fantasy-filter": encoded_filter}
        url = f"{self.config.ffl_api_base}/seasons/{year}/players"
        payload = self._get_json(url, params=params, headers=headers)
        if not isinstance(payload, list):
            raise ESPNResponseError("ESPN returned an unexpected player payload")
        return payload
