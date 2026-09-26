"""Bridge configuration: a TOML file (config/bridge.toml) over built-in defaults.

Everything site-specific lives here, not in code: which assigning authority is the MRN, what URI each
assigning authority becomes, the local time zone for timestamps sent without an offset.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass
class Config:
    # listener
    host: str = "127.0.0.1"
    port: int = 2575
    max_message_bytes: int = 10 * 1024 * 1024
    idle_timeout_seconds: float = 300.0

    # hl7
    accepted_versions: tuple[str, ...] = ("2.3", "2.3.1", "2.4", "2.5", "2.5.1")
    default_charset: str = "utf-8"
    fallback_charsets: tuple[str, ...] = ("cp1252", "latin-1")
    unsupported_messages: str = "ack"          # "ack": AA + warning (don't block the sender's queue); "reject": AR
    dedupe_cache_size: int = 10000
    receiving_application: str = "V2FHIR"
    receiving_facility: str = "BRIDGE"

    # output
    out_dir: str = ""                          # write each bundle here (empty: don't)
    fhir_base_url: str = ""                    # POST each bundle here (empty: don't)
    fhir_timeout_seconds: float = 15.0
    fhir_retries: int = 2
    validate: bool = True                      # validate every bundle with fhir.resources before sending

    # mapping
    default_timezone: str = "America/Chicago"  # for v2 timestamps without an offset; "" = truncate to date
    identifier_system_base: str = "http://example.org/fhir/sid/"
    code_system_base: str = "http://example.org/fhir/CodeSystem/"
    mrn_authorities: tuple[str, ...] = ()      # PID-3 assigning authorities that issue the MRN, in priority order
    default_assigning_authority: str = ""      # assumed when PID-3.4 is empty
    assigning_authorities: dict[str, str] = field(default_factory=dict)   # namespace id -> Identifier.system

    @property
    def tz(self) -> ZoneInfo | None:
        if not self.default_timezone:
            return None
        try:
            return ZoneInfo(self.default_timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown time zone {self.default_timezone!r} (on Windows: pip install tzdata)") from e


_SECTIONS = ("listener", "hl7", "output", "mapping")


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> Config:
    values: dict = {}
    if path:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        for section in _SECTIONS:
            values.update(data.get(section, {}))
    values.update(overrides or {})
    known = {f.name: f for f in fields(Config)}
    unknown = set(values) - set(known)
    if unknown:
        raise ValueError(f"unknown config keys: {', '.join(sorted(unknown))}")
    for k, v in list(values.items()):
        if isinstance(v, list):
            values[k] = tuple(v)
    cfg = Config(**values)
    if cfg.unsupported_messages not in ("ack", "reject"):
        raise ValueError("unsupported_messages must be 'ack' or 'reject'")
    _ = cfg.tz   # fail at startup, not on the first message
    return cfg
