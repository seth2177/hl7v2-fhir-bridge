from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from ..config import Config
from ..errors import Issue, Location
from ..hl7.parser import Message
from .bundle import TransactionBuilder


@dataclass
class Ctx:
    """Everything one conversion needs, plus the warnings it collects."""
    msg: Message
    cfg: Config
    now: datetime
    tx: TransactionBuilder = field(default_factory=TransactionBuilder)
    warnings: list[Issue] = field(default_factory=list)
    _tz_assumed: int = 0
    _tz_first: tuple[str, Location | None] | None = None

    def __post_init__(self):
        self._tz = self.cfg.tz

    @property
    def tz(self) -> ZoneInfo | None:
        return self._tz

    def warn(self, text: str, where: Location | None = None, code: str = "0") -> None:
        issue = Issue(code, text, where, "W")
        if issue not in self.warnings:       # the same provider in PV1-7 and ORC-12 would warn twice; the ACK has 5 ERRs
            self.warnings.append(issue)

    def note_assumed_tz(self, value: str, where: Location | None) -> None:
        self._tz_assumed += 1
        if self._tz_first is None:
            self._tz_first = (value, where)

    def finish(self) -> None:
        if self._tz_assumed:
            value, where = self._tz_first
            self.warn(f"{self._tz_assumed} timestamp(s) without UTC offset (first: {value}); assumed {self.cfg.default_timezone}", where)

    # ---- provenance helpers ------------------------------------------------------------------------
    @property
    def message_system(self) -> str:
        """Identifier system for this sender's MSH-10 values (control ids are only unique per sender)."""
        return f"urn:hl7v2:{quote(self.msg.sending_app, safe='')}:{quote(self.msg.sending_facility, safe='')}"

    @property
    def source_uri(self) -> str:
        """meta.source on every resource: the message that created or last replaced this version (a status-only
        PATCH keeps the previous one; its Provenance names the patching message)."""
        return f"{self.message_system}#{quote(self.msg.control_id, safe='')}"

    @property
    def provenance_id(self) -> str:
        # With the content hash, a resend (even with a fresh MSH-7) rewrites its own Provenance, and a different
        # message that reuses the control id (a counter reset) gets a new one.
        key = f"{self.msg.sending_app}|{self.msg.sending_facility}|{self.msg.control_id}|{self.msg.content_digest}"
        h = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()
        return f"v2-{h[:40]}"

    def meta(self) -> dict:
        return {"source": self.source_uri}
