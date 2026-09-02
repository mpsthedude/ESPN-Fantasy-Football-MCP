"""Project-owned ESPN authentication/session state.

This module owns only credential lifecycle and construction of the project's
HTTP transport. It never constructs third-party ESPN wrapper objects and never
caches league object graphs.
"""

from __future__ import annotations

import app_config
from espn_transport import ESPNTransport


class ESPNSessionManager:
    """Small credential context for one or more logical MCP sessions."""

    def __init__(self) -> None:
        self.credentials: dict[str, dict[str, str]] = {}
        self._configuration_checked: set[str] = set()

    def store_credentials(self, session_key: str, espn_s2: str, swid: str) -> None:
        values = (espn_s2, swid)
        if any(isinstance(value, str) and value.strip().upper() == "ENV" for value in values):
            raise ValueError(
                "ENV is not an ESPN cookie value. Environment authentication is automatic; "
                "do not use placeholder text as a credential."
            )
        self.credentials[session_key] = {"espn_s2": espn_s2, "swid": swid}

    def clear_credentials(self, session_key: str) -> bool:
        return self.credentials.pop(session_key, None) is not None

    def prime(self, session_key: str) -> None:
        """Resolve configured credentials once for this logical session."""
        if session_key in self.credentials or session_key in self._configuration_checked:
            return
        self._configuration_checked.add(session_key)
        resolved = app_config.resolve_espn_credentials()
        if resolved is not None:
            espn_s2, swid, _source = resolved
            self.store_credentials(session_key, espn_s2, swid)

    def get_transport(self, session_key: str) -> ESPNTransport:
        self.prime(session_key)
        creds = self.credentials.get(session_key) or {}
        return ESPNTransport(creds.get("espn_s2"), creds.get("swid"))
