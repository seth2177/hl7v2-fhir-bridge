"""The bridge service: bytes in, ACK out. Parse -> dedupe -> convert -> validate -> sink -> ACK.

`Bridge.handle()` never raises. Whatever arrives, the sender gets an ACK it can act on:

  AA  accepted (possibly with ERR warnings on v2.5+)
  AR  rejected: unparseable, unsupported version, (optionally) unsupported message type
  AE  error: content can't be mapped, generated FHIR invalid, FHIR server refused or unreachable

The ACK is only AA once the bundle is on disk / accepted by the FHIR server, so an AA really means
"you can forget this message". On AE the sender keeps it and can retry; retries are safe because the
bundle is idempotent.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .convert import UnsupportedMessage, convert
from .errors import APPLICATION_INTERNAL_ERROR, UNKNOWN_KEY_IDENTIFIER, UNSUPPORTED_VERSION_ID, HL7Error, Issue, Location, RejectError
from .hl7.ack import build_ack, head_fields
from .hl7.parser import Message, parse_bytes
from .sink import DirectorySink, EntryResult, FhirError, FhirSink, safe_name
from .validate import BundleInvalid, validate_bundle

log = logging.getLogger("v2fhir")
MAX_ERR_SEGMENTS = 5
EARLY_REPORT = {"registered", "partial", "preliminary"}
SIGNED_REPORT = {"final", "amended", "corrected", "appended"}


@dataclass
class Result:
    ack: str | None                       # None: nothing to send (we were sent an ACK)
    ack_code: str
    message_type: str = ""
    control_id: str = ""
    entries: list[EntryResult] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    duplicate: bool = False
    bundle_path: Path | None = None
    bundle: dict | None = None
    charset: str = "utf-8"

    @property
    def ack_bytes(self) -> bytes | None:
        if self.ack is None:
            return None
        return self.ack.encode(self.charset, errors="replace")


class Bridge:
    def __init__(self, cfg: Config, *, fhir_sink: FhirSink | None = None, clock: Callable[[], datetime] | None = None,
                 on_result: Callable[[Result], None] | None = None):
        self.cfg = cfg
        self.dir_sink = DirectorySink(cfg.out_dir) if cfg.out_dir else None
        self.fhir_sink = fhir_sink or (FhirSink(cfg.fhir_base_url, cfg.fhir_timeout_seconds, cfg.fhir_retries) if cfg.fhir_base_url else None)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.on_result = on_result
        self._seen: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self._lock = threading.Lock()

    # ---- entry points -----------------------------------------------------------------------------
    def handle(self, raw: bytes) -> Result:
        try:
            result = self._handle(raw)
        except Exception as e:  # noqa: BLE001 -- the listener must never die and the sender must get an answer
            log.exception("internal error handling message")
            result = self._nak_raw(raw, HL7Error(f"internal error: {type(e).__name__}", APPLICATION_INTERNAL_ERROR))
        level = logging.INFO if result.ack_code == "AA" else logging.WARNING
        log.log(level, "%s %s -> %s%s", result.message_type or "?", result.control_id or "?", result.ack_code,
                "".join(f" | {i}" for i in result.issues[:3]))
        if self.on_result:
            try:
                self.on_result(result)
            except Exception:  # noqa: BLE001
                log.exception("on_result callback failed")
        return result

    def handle_oversize(self, head: bytes, size: int) -> Result:
        """The MLLP layer dropped a frame larger than max_message_bytes; NAK it using what we kept of MSH."""
        err = RejectError(f"message of {size} bytes exceeds the {self.cfg.max_message_bytes}-byte limit", APPLICATION_INTERNAL_ERROR)
        result = self._nak_raw(head, err)
        log.warning("oversize message (%d bytes) rejected: %s", size, result.control_id or "?")
        if self.on_result:
            self.on_result(result)
        return result

    def close(self) -> None:
        if self.fhir_sink:
            self.fhir_sink.close()

    # ---- pipeline ---------------------------------------------------------------------------------
    def _handle(self, raw: bytes) -> Result:
        cfg = self.cfg
        try:
            msg = parse_bytes(raw, cfg.default_charset, cfg.fallback_charsets)
        except HL7Error as e:
            return self._nak_raw(raw, e)

        if msg.message_type.upper() == "ACK":
            return Result(None, "", "ACK", msg.control_id)          # never acknowledge an acknowledgement
        if msg.version not in cfg.accepted_versions:
            return self._nak(msg, RejectError(f"HL7 version {msg.version} not accepted (accepted: {', '.join(cfg.accepted_versions)})",
                                              UNSUPPORTED_VERSION_ID, Location("MSH", 1, 12)))

        key = (msg.sending_app, msg.sending_facility, msg.control_id)
        digest = _digest(msg)
        extra: list[Issue] = []
        with self._lock:
            prior = self._seen.get(key)
        if prior == digest:
            return self._ack(msg, "AA", [Issue("0", "duplicate of an already-accepted message; not sent again", Location("MSH", 1, 10), "W")],
                             duplicate=True)
        if prior is not None:
            extra.append(Issue("0", "control id reused with different content; processed as a new message", Location("MSH", 1, 10), "W"))

        try:
            conv = convert(msg, cfg, self.clock())
        except UnsupportedMessage as e:
            if cfg.unsupported_messages == "ack":
                return self._ack(msg, "AA", [Issue(e.issue.code, f"{e.issue.text}; acknowledged and ignored", e.issue.location, "W")])
            return self._nak(msg, e)
        except HL7Error as e:
            return self._nak(msg, e)

        bundle = conv.bundle
        if cfg.validate:
            try:
                validate_bundle(bundle)
            except BundleInvalid as e:
                log.error("generated bundle failed validation: %s", e)
                return self._nak(msg, HL7Error(f"generated FHIR failed validation: {e}"[:250], APPLICATION_INTERNAL_ERROR), bundle=bundle)

        entries: list[EntryResult] = []
        path = None
        try:
            if self.fhir_sink:
                extra += self._drop_late_preliminary(bundle)
            path = self.dir_sink.write(bundle, safe_name(msg.sending_app, msg.control_id)) if self.dir_sink else None
            if self.fhir_sink:
                entries = self.fhir_sink.post(bundle)
        except FhirError as e:
            has_patch = any(en["request"]["method"] == "PATCH" for en in bundle["entry"])
            if has_patch and e.status == 404:
                err = HL7Error(f"order not found on the FHIR server, nothing to update ({e})"[:250], UNKNOWN_KEY_IDENTIFIER)
            elif e.status == 412:
                err = HL7Error(f"identifiers match more than one resource; needs manual reconciliation ({e})"[:250], APPLICATION_INTERNAL_ERROR)
            else:
                err = HL7Error(str(e)[:250] + ("; will be safe to resend" if e.retryable else ""), APPLICATION_INTERNAL_ERROR)
            return self._nak(msg, err, bundle=bundle, path=path)

        issues = extra + _status_change_for_unknown_order(bundle, entries) + conv.warnings
        with self._lock:
            self._seen[key] = digest
            self._seen.move_to_end(key)
            while len(self._seen) > cfg.dedupe_cache_size:
                self._seen.popitem(last=False)
        return self._ack(msg, "AA", issues, entries=entries, bundle=bundle, path=path)

    def _drop_late_preliminary(self, bundle: dict) -> list[Issue]:
        """A preliminary report arriving after the final one (an interface queue retried an old message)
        must not turn a signed report back into a preliminary. A conditional PUT cannot say "only if not
        final", so the bridge reads the current report first and drops the stale DiagnosticReport entry.
        (Read-then-write: safe for one connection sending in order; see README for concurrent senders.)"""
        issues = []
        for e in list(bundle["entry"]):
            res, req = e["resource"], e["request"]
            if res.get("resourceType") != "DiagnosticReport" or req["method"] != "PUT" or res.get("status") not in EARLY_REPORT:
                continue
            current = self.fhir_sink.search(req["url"])
            if any(c.get("status") in SIGNED_REPORT for c in current):
                bundle["entry"].remove(e)
                for other in bundle["entry"]:
                    if other["resource"].get("resourceType") == "Provenance":
                        other["resource"]["target"] = [t for t in other["resource"]["target"] if t.get("reference") != e["fullUrl"]]
                issues.append(Issue("0", f"late {res['status']} report ignored: the report is already {current[0].get('status')}",
                                    Location("OBR", 1, 25), "W"))
        return issues

    # ---- ACK helpers -------------------------------------------------------------------------------
    def _ack(self, msg: Message, code: str, issues: list[Issue], *, entries=None, bundle=None, path=None, duplicate=False) -> Result:
        text = build_ack(code, msg=msg, issues=issues[:MAX_ERR_SEGMENTS], receiving_app=self.cfg.receiving_application,
                         receiving_facility=self.cfg.receiving_facility)
        return Result(text, code, msg.type_label, msg.control_id, entries or [], issues, duplicate, path, bundle, _ack_charset(msg))

    def _nak(self, msg: Message, err: HL7Error, *, bundle=None, path=None) -> Result:
        return self._ack(msg, err.ack_code, [err.issue], bundle=bundle, path=path)

    def _nak_raw(self, raw: bytes, err: HL7Error) -> Result:
        head = head_fields(raw)
        text = build_ack(err.ack_code, head=head, issues=[err.issue], receiving_app=self.cfg.receiving_application,
                         receiving_facility=self.cfg.receiving_facility)
        mt = head.get("9.2", "")
        return Result(text, err.ack_code, f"?^{mt}" if mt else "", head.get("10", ""), [], [err.issue])


def _digest(msg: Message) -> str:
    """Content hash ignoring MSH-7, so a resend with a fresh timestamp still counts as the same message."""
    fields = list(msg.msh.fields)
    if len(fields) > 7:
        fields[7] = ""
    body = "\r".join([ "|".join(fields) ] + [str(s) for s in msg.segments[1:]])
    return hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()


def _ack_charset(msg: Message) -> str:
    return msg.charset if msg.charset not in ("ascii",) else "ascii"


def _status_change_for_unknown_order(bundle: dict, entries: list[EntryResult]) -> list[Issue]:
    """A cancel or status change (full ORC+OBR) for an order the server never had creates it with that
    status, so the change isn't lost. Worth telling someone: the NW probably went missing upstream."""
    out = []
    for req, res in zip(bundle.get("entry", []), entries, strict=False):
        r = req.get("resource", {})
        if r.get("resourceType") == "ServiceRequest" and req["request"]["method"] == "PUT" and res.outcome == "created":
            ident = r["identifier"][0]["value"]
            out.append(Issue(UNKNOWN_KEY_IDENTIFIER, f"status change ({r.get('status')}) for unknown order {ident}; created it",
                             Location("ORC", 1, 2), "W"))
    return out
