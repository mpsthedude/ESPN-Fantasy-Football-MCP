"""draft_strategy_store.py - Draft Intelligence Phase D2

Pure local persistence for pre-draft strategy artifacts. This module
handles ONLY:
  - deterministic strategy file naming
  - schema/version validation
  - atomic save (temp file + os.replace, no half-written files)
  - load current strategy
  - safe replacement (one active strategy per league_id+year)

It has NO ESPN access, NO FantasyPros access, and NO analytical
methodology - that all lives in espn_fantasy_server.py's
prepare_draft_strategy. This mirrors the same separation-of-concerns
principle as commissioner_config.py (config/guard only, never analysis).

Strategy files are LOCAL, NON-SECRET analytical artifacts. They must
NEVER contain espn_s2, SWID, FantasyPros API keys, cookies, or auth
headers - callers are responsible for not passing secrets in, but this
module also defensively scans for common secret-shaped keys before
writing as a last line of defense (see _scan_for_forbidden_keys).
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

import app_config

STRATEGY_SCHEMA_VERSION = 2  # current write version - new saves always use this
SUPPORTED_STRATEGY_SCHEMA_VERSIONS = (1, 2)  # D2.1: v1 remains loadable/valid, never auto-migrated

# D3D-B: LEGACY_STORE_DIR is the pre-D3D-B source-relative directory,
# preserved exactly as before for read-fallback purposes only (per-file,
# never per-directory - see _resolve_read_dir). New default writes never
# target this directory; the new authoritative default is
# app_config.get_draft_strategy_dir(). Explicit strategy_dir arguments
# bypass both defaults entirely.
LEGACY_STORE_DIR = Path(__file__).resolve().parent / ".draft_strategy"
_STORE_DIR = LEGACY_STORE_DIR  # kept as an alias; no external caller references this name

_FORBIDDEN_KEY_SUBSTRINGS = (
    "espn_s2", "swid", "fantasypros_api_key", "api_key", "cookie",
    "authorization", "auth_header", "session_id", "secret",
)


class DraftStrategyStoreError(Exception):
    """Raised for any validation/persistence failure. Callers should
    catch this and return a structured error - never let it propagate
    as a raw traceback to an MCP tool response."""
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _strategy_path(league_id: int, year: int, directory: Path) -> Path:
    return Path(directory) / f"{league_id}_{year}.json"


def _resolve_read_dir(league_id: int, year: int, strategy_dir=None) -> Path:
    """Resolves which directory to READ the given league_id/year strategy
    file from (D3D-B).

    If strategy_dir is explicitly supplied, it is used exactly - no
    fallback of any kind, even if the file is missing there.

    Otherwise, resolution is PER STRATEGY FILE (never a blanket
    "does the new directory exist" check, since the new directory may
    exist for other league/year files while the requested one only
    exists in legacy storage):
        1. The new app-home directory, if the exact
           "{league_id}_{year}.json" file exists there. A malformed file
           at this location is NOT masked by this function - the caller
           (load_strategy) will raise on the malformed content, and this
           resolver never falls through to legacy in that case, because
           it has already committed to the new directory once the file's
           presence there is confirmed.
        2. LEGACY_STORE_DIR, only if the new file does not exist at all
           but the legacy file does.
        3. The new app-home directory by default when neither file
           exists, so missing-file callers (load_strategy returning None,
           strategy_exists returning False) report against the current/
           expected location conceptually, without needing to raise here.
    """
    if strategy_dir is not None:
        return Path(strategy_dir)
    new_dir = app_config.get_draft_strategy_dir()
    if (new_dir / f"{league_id}_{year}.json").exists():
        return new_dir
    if (LEGACY_STORE_DIR / f"{league_id}_{year}.json").exists():
        return LEGACY_STORE_DIR
    return new_dir


def _scan_for_forbidden_keys(obj, path="root") -> Optional[str]:
    """Recursively scans a dict/list structure for any key whose name
    (lowercased) contains a forbidden secret-shaped substring. Returns
    the offending path string, or None if clean. This is a DEFENSIVE
    check only - callers must never intentionally pass credentials in;
    this exists so a future coding mistake cannot silently write
    secrets to disk."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            for bad in _FORBIDDEN_KEY_SUBSTRINGS:
                if bad in kl:
                    return f"{path}.{k}"
            found = _scan_for_forbidden_keys(v, f"{path}.{k}")
            if found:
                return found
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found = _scan_for_forbidden_keys(item, f"{path}[{i}]")
            if found:
                return found
    return None


def _recompute_structural_fingerprint(structural_inputs: dict) -> str:
    """Deterministic SHA-256 of canonical JSON over structural_inputs ONLY.
    Mirrors the exact canonicalization convention used by D2's own
    _ds_build_input_fingerprint (sort_keys, default=str) so this module
    never diverges from the analytical engine's own hashing approach.
    D2.1 scope note: this helper only RECOMPUTES/VALIDATES a fingerprint
    already computed by the analytical engine - it contains no strategy
    methodology itself."""
    canonical = json.dumps(structural_inputs, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_strategy_document(doc: dict, expected_league_id: int = None, expected_year: int = None) -> None:
    """Raises DraftStrategyStoreError on any structural problem. Never
    silently accepts a malformed or wrong-scope document.

    D2.1: supports BOTH schema versions 1 and 2. v1 documents (pre-D2.1,
    including any already on disk) remain fully valid and loadable AS-IS -
    never auto-migrated or rewritten by this function. v2 documents
    additionally require structural_inputs + structural_fingerprint, and
    the fingerprint MUST recompute-match the stored structural_inputs -
    a mismatch is treated as corruption, never silently accepted."""
    if not isinstance(doc, dict):
        raise DraftStrategyStoreError("malformed_document", "Strategy document is not a JSON object.")
    for required_key in ("schema_version", "league_id", "year", "strategy_id", "input_fingerprint", "created_at_utc"):
        if required_key not in doc:
            raise DraftStrategyStoreError("missing_field", f"Strategy document missing required field: {required_key}")
    if doc["schema_version"] not in SUPPORTED_STRATEGY_SCHEMA_VERSIONS:
        raise DraftStrategyStoreError(
            "unsupported_version",
            f"Strategy schema_version {doc['schema_version']} is not supported "
            f"(supported: {SUPPORTED_STRATEGY_SCHEMA_VERSIONS})."
        )
    if expected_league_id is not None and doc["league_id"] != expected_league_id:
        raise DraftStrategyStoreError(
            "league_mismatch",
            f"Strategy league_id {doc['league_id']} does not match expected {expected_league_id}."
        )
    if expected_year is not None and doc["year"] != expected_year:
        raise DraftStrategyStoreError(
            "year_mismatch", f"Strategy year {doc['year']} does not match expected {expected_year}."
        )
    if not doc["input_fingerprint"] or not isinstance(doc["input_fingerprint"], str):
        raise DraftStrategyStoreError("invalid_fingerprint", "input_fingerprint must be a non-empty string.")

    if doc["schema_version"] >= 2:
        if "structural_inputs" not in doc or not isinstance(doc["structural_inputs"], dict):
            raise DraftStrategyStoreError("missing_field", "v2 strategy document missing structural_inputs.")
        if "structural_fingerprint" not in doc or not isinstance(doc["structural_fingerprint"], str) or not doc["structural_fingerprint"]:
            raise DraftStrategyStoreError("missing_field", "v2 strategy document missing structural_fingerprint.")
        recomputed = _recompute_structural_fingerprint(doc["structural_inputs"])
        if recomputed != doc["structural_fingerprint"]:
            raise DraftStrategyStoreError(
                "structural_fingerprint_mismatch",
                "v2 strategy document's structural_fingerprint does not match its own structural_inputs - "
                "treated as corruption, never silently accepted."
            )


def save_strategy(league_id: int, year: int, strategy_doc: dict, *, strategy_dir=None) -> Path:
    """Atomically persists strategy_doc as the SOLE current strategy for
    league_id+year. Overwrites any previous strategy for the same
    league_id+year (one active strategy per league+year, per D2 design -
    no uncontrolled timestamped accumulation). Uses write-temp-then-
    os.replace so a crash mid-write never leaves a half-written file.

    D3D-B write destination: if strategy_dir is explicitly supplied, that
    exact directory is used (and created if needed) - no fallback of any
    kind. Otherwise, ALL default writes go to app_config.get_draft_strategy_dir()
    regardless of whether a legacy file/directory exists, is empty, or is
    absent - this is the intentional one-way forward-migration mechanism:
    the legacy directory is NEVER written to, copied from, or modified by
    this function under any default-argument circumstance."""
    validate_strategy_document(strategy_doc, expected_league_id=league_id, expected_year=year)

    forbidden_path = _scan_for_forbidden_keys(strategy_doc)
    if forbidden_path:
        raise DraftStrategyStoreError(
            "forbidden_content", f"Strategy document contains a secret-shaped key at {forbidden_path} - refusing to persist."
        )

    target_dir = Path(strategy_dir) if strategy_dir is not None else app_config.get_draft_strategy_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    final_path = _strategy_path(league_id, year, target_dir)

    fd, tmp_path = tempfile.mkstemp(prefix=f".{league_id}_{year}_", suffix=".tmp", dir=str(target_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(strategy_doc, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, final_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise
    return final_path


def load_strategy(league_id: int, year: int, *, strategy_dir=None) -> Optional[dict]:
    """Returns the current persisted strategy dict for league_id+year, or
    None if no strategy file exists. Raises DraftStrategyStoreError on
    malformed JSON or a document that fails validation - never returns
    silently-corrupted data.

    D3D-B read resolution: see _resolve_read_dir. A malformed file found
    at the new (or explicitly supplied) location raises here and NEVER
    falls back to a legacy file - presence of the new file makes it
    authoritative. Never creates any directory."""
    read_dir = _resolve_read_dir(league_id, year, strategy_dir)
    path = _strategy_path(league_id, year, read_dir)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise DraftStrategyStoreError("malformed_json", f"Strategy file for {league_id}_{year} is not valid JSON: {e}")
    validate_strategy_document(doc, expected_league_id=league_id, expected_year=year)
    return doc


def strategy_exists(league_id: int, year: int, *, strategy_dir=None) -> bool:
    """D3D-B: follows the identical per-file new-then-legacy resolution as
    load_strategy (see _resolve_read_dir). Never creates any directory."""
    read_dir = _resolve_read_dir(league_id, year, strategy_dir)
    return _strategy_path(league_id, year, read_dir).exists()
