"""The bridge service: bytes in, ACK out. Parse -> dedupe -> convert -> validate -> sink -> ACK.

`Bridge.handle()` never raises. Whatever arrives, the sender gets an ACK it can act on:

  AA  accepted (possibly with ERR warnings on v2.5+)
  AR  rejected for reasons unrelated to the content: unparseable, unsupported version or processing id,
      (optionally) unsupported message type; and, by default, FHIR server down or an internal error, where
      v2.5.1 2.9.2.2 says the sender should resend later (transient_failure_ack = "AE" to change that)
  AE  error in the content: can't be mapped, generated FHIR invalid, FHIR server refused it (4xx)

The ACK is only AA once the bundle is on disk / accepted by the FHIR server, so an AA really means
"you can forget this message". On AR/AE the sender keeps it; a resend is safe because every request is
conditional and orders and reports are checked before writing.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .convert import UnsupportedMessage, convert
from .errors import (
    APPLICATION_INTERNAL_ERROR,
    DUPLICATE_KEY_IDENTIFIER,
    REQUIRED_FIELD_MISSING,
    UNKNOWN_KEY_IDENTIFIER,
    UNSUPPORTED_PROCESSING_ID,
    UNSUPPORTED_VERSION_ID,
    HL7Error,
    Issue,
    Location,
    RejectError,
)
from .hl7.ack import build_ack, head_fields
from .hl7.charset import CHARSETS, declared_charset, strip_bom
from .hl7.parser import Message, parse_bytes
from .mapping import tables as T
from .mapping.bundle import token
from .sink import DirectorySink, EntryResult, FhirError, FhirSink, safe_name
from .validate import BundleInvalid, validate_bundle

log = logging.getLogger("v2fhir")
MAX_ERR_SEGMENTS = 5


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
        # FHIR work (the reads before writing and the transaction) runs one message at a time across all
        # connections: the read-then-write checks stay consistent, and radiology volume doesn't need parallel
        # writes. Waiting for the lock counts against ack_deadline_seconds like everything else.
        self._fhir_lock = threading.Lock()
        self._deadline = threading.local()

    # ---- entry points -----------------------------------------------------------------------------
    def handle(self, raw: bytes) -> Result:
        try:
            result = self._handle(raw)
        except Exception as e:  # noqa: BLE001 -- the listener must never die and the sender must get an answer
            log.exception("internal error handling message")
            result = self._nak_raw(raw, self._transient(f"internal error: {type(e).__name__}"))
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
        self._deadline.at = time.monotonic() + cfg.ack_deadline_seconds     # this message's ACK is due by then
        try:
            msg = parse_bytes(raw, cfg.default_charset, cfg.fallback_charsets)
        except HL7Error as e:
            return self._nak_raw(raw, e)

        if msg.message_type.upper() == "ACK":
            return Result(None, "", "ACK", msg.control_id)          # never acknowledge an acknowledgement
        if msg.version not in cfg.accepted_versions:
            return self._nak(msg, RejectError(f"HL7 version {msg.version} not accepted (accepted: {', '.join(cfg.accepted_versions)})",
                                              UNSUPPORTED_VERSION_ID, Location("MSH", 1, 12)))
        processing = (msg.msh.get(11) or "").upper()
        if not processing:
            return self._nak(msg, RejectError("MSH-11 (processing id) is empty", REQUIRED_FIELD_MISSING, Location("MSH", 1, 11)))
        if processing not in cfg.accepted_processing_ids:                # a training or test feed pointed at production
            return self._nak(msg, RejectError(f"processing id {processing} not accepted (accepted: {', '.join(cfg.accepted_processing_ids)})",
                                              UNSUPPORTED_PROCESSING_ID, Location("MSH", 1, 11)))

        key = (msg.sending_app, msg.sending_facility, msg.control_id)
        digest = msg.content_digest
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
        locked = False
        try:
            if self.fhir_sink:
                locked = self._fhir_lock.acquire(timeout=max(0.0, self._deadline.at - time.monotonic()))
                if not locked:
                    raise FhirError("FHIR writes queued behind another connection past the ACK deadline", None, True)
                extra += self._reconcile(bundle)
                if msg.message_type.upper() == "ADT" and msg.trigger.upper() != "A40":
                    extra += self._keep_merges(bundle)
            # The content hash (MSH-7 ignored) keeps an identical resend on its own file but gives a reused control id a new one.
            path = self.dir_sink.write(bundle, f"{safe_name(msg.sending_app, msg.sending_facility, msg.control_id)}-{digest[:12]}") if self.dir_sink else None
            if self.fhir_sink:
                entries = self.fhir_sink.post(bundle, deadline=self._deadline.at)
        except HL7Error as e:
            return self._nak(msg, e, bundle=bundle, path=path)
        except FhirError as e:
            has_patch = any(en["request"]["method"] == "PATCH" for en in bundle["entry"])
            if has_patch and e.status == 404:
                err = HL7Error(f"order not found on the FHIR server, nothing to update ({e})"[:250], UNKNOWN_KEY_IDENTIFIER)
            elif e.status == 412:
                err = HL7Error(f"identifiers match more than one resource; needs manual reconciliation ({e})"[:250], APPLICATION_INTERNAL_ERROR)
            elif e.retryable:                  # down, timed out, 5xx: nothing wrong with the message itself
                err = self._transient(str(e)[:250] + "; will be safe to resend")
            else:
                err = HL7Error(str(e)[:250], APPLICATION_INTERNAL_ERROR)
            return self._nak(msg, err, bundle=bundle, path=path)
        finally:
            if locked:
                self._fhir_lock.release()

        issues = extra + _status_change_for_unknown_order(bundle, entries) + conv.warnings
        with self._lock:
            self._seen[key] = digest
            self._seen.move_to_end(key)
            while len(self._seen) > cfg.dedupe_cache_size:
                self._seen.popitem(last=False)
        return self._ack(msg, "AA", issues, entries=entries, bundle=bundle, path=path)

    def _reconcile(self, bundle: dict) -> list[Issue]:
        """Orders and reports: read what the server holds before writing, because a conditional request can say
        neither "only if newer" nor "this single match is really a different order". For each ServiceRequest or
        DiagnosticReport entry that matches exactly one stored resource:
          conflict  a stored order number of the same type and system has another value: a different order
                    shares one of the numbers -> AE 205, a person has to look
          patient   the stored order or report belongs to another patient (not this one, nor one linked to it by
                    an A40 merge) -> AE 207, a person has to look
          stale     an older message would undo newer state (a final over a correction, a late preliminary over
                    a final, a status change that re-opens a cancelled order) -> the entry is dropped, with a warning
          merge     numbers the server has but this message lacks are kept, and so is an order's authoredOn;
                    a create-if-absent that matches an order lacking some of this message's numbers adds them
        Read-then-write: safe for one connection sending in order; see README for concurrent senders."""
        issues = []
        for e in list(bundle["entry"]):
            res, req = e["resource"], e["request"]
            rt = req["url"].split("?")[0].split("/")[0]
            if rt not in ("ServiceRequest", "DiagnosticReport"):
                continue
            query = f"{rt}?{req['ifNoneExist']}" if req["method"] == "POST" else req["url"]
            if "?" not in query:
                continue
            current = self._search(query)
            if len(current) != 1:
                continue                      # none: it is created; several: the server answers 412 -> AE
            cur = current[0]
            if req["method"] != "PATCH":
                clash = _conflicting_number(res.get("identifier", []), cur.get("identifier", []))
                if clash:
                    raise HL7Error(f"{rt}/{cur.get('id')} has {clash}: a different order shares one of these numbers; "
                                   "needs manual reconciliation"[:250], DUPLICATE_KEY_IDENTIFIER,
                                   Location("ORC" if rt == "ServiceRequest" else "OBR", 1, 2))
                if not self._same_patient(bundle, res, cur):
                    raise HL7Error(f"{rt}/{cur.get('id')} matched on its order numbers belongs to another patient "
                                   f"({(cur.get('subject') or {}).get('reference')}); needs manual reconciliation"[:250],
                                   APPLICATION_INTERNAL_ERROR, Location("PID", 1, 3))
            stale = _stale(rt, req, res, cur)
            if stale:
                _drop_entry(bundle, e)
                issues.append(Issue("0", stale, Location("OBR", 1, 25) if rt == "DiagnosticReport" else Location("ORC", 1, 1), "W"))
                continue
            if req["method"] != "PATCH":
                _merge_numbers(e, cur, query)
        return issues

    def _keep_merges(self, bundle: dict) -> list[Issue]:
        """An A40 leaves link entries on both patients, but PID carries no links, so the next A08's PUT would
        erase them, and an A08 from a feed that still uses the merged-away MRN would re-activate that record.
        Read the current Patient first: keep its links, and don't touch a record that was merged away."""
        issues = []
        for e in bundle["entry"]:
            res, req = e["resource"], e["request"]
            if res.get("resourceType") != "Patient" or req["method"] != "PUT" or "?" not in req["url"]:
                continue
            current = self._search(req["url"])
            if len(current) != 1:
                continue
            cur = current[0]
            merged_into = [ln for ln in cur.get("link", []) if ln.get("type") == "replaced-by"]
            if cur.get("active") is False and merged_into:
                e["request"] = {"method": "POST", "url": "Patient", "ifNoneExist": req["url"].split("?", 1)[1]}
                issues.append(Issue("0", f"patient was merged into {merged_into[0].get('other', {}).get('reference')}; "
                                         "demographics not applied", Location("PID", 1, 3), "W"))
            elif cur.get("link") and not res.get("link"):
                res["link"] = cur["link"]
        return issues

    def _search(self, query: str) -> list[dict]:
        return self.fhir_sink.search(query, deadline=self._deadline.at)

    def _same_patient(self, bundle: dict, res: dict, cur: dict) -> bool:
        """Is the stored resource's subject this message's patient, or a patient an A40 linked to it?"""
        stored = (cur.get("subject") or {}).get("reference", "")
        mine = next((e for e in bundle["entry"] if e["fullUrl"] == (res.get("subject") or {}).get("reference")), None)
        if not stored.startswith("Patient/") or mine is None:
            return True                       # nothing to compare
        req = mine["request"]
        query = req["url"].split("?", 1)[1] if "?" in req["url"] else req.get("ifNoneExist")
        if not query:
            return True
        mine_ids = {p.get("id") for p in self._search(f"Patient?{query}")}
        sid = stored.split("/", 1)[1]
        linked = {sid}
        for p in self._search(f"Patient?_id={sid}"):
            linked |= {(ln.get("other") or {}).get("reference", "").split("/", 1)[-1] for ln in p.get("link", [])}
        return bool(mine_ids & linked)

    # ---- ACK helpers -------------------------------------------------------------------------------
    def _transient(self, text: str) -> HL7Error:
        """System down or internal error: AR by default ("resend later", v2.5.1 2.9.2.2), AE if configured."""
        err = HL7Error(text, APPLICATION_INTERNAL_ERROR)
        err.ack_code = self.cfg.transient_failure_ack
        return err

    def _ack(self, msg: Message, code: str, issues: list[Issue], *, entries=None, bundle=None, path=None, duplicate=False) -> Result:
        text = build_ack(code, msg=msg, issues=issues[:MAX_ERR_SEGMENTS], receiving_app=self.cfg.receiving_application,
                         receiving_facility=self.cfg.receiving_facility)
        return Result(text, code, msg.type_label, msg.control_id, entries or [], issues, duplicate, path, bundle, _ack_charset(msg))

    def _nak(self, msg: Message, err: HL7Error, *, bundle=None, path=None) -> Result:
        return self._ack(msg, err.ack_code, [err.issue], bundle=bundle, path=path)

    def _nak_raw(self, raw: bytes, err: HL7Error) -> Result:
        """NAK for bytes that didn't parse. MSH is read, and the NAK written, in the character set MSH-18
        declares; with none declared, MSH is read as UTF-8 if valid (else Latin-1) and the NAK is UTF-8."""
        raw = strip_bom(raw)
        declared = declared_charset(raw)
        codec = CHARSETS.get(declared.upper()) if declared else None
        read_as = codec
        if read_as is None:
            try:
                raw.decode("utf-8")
                read_as = "utf-8"
            except UnicodeDecodeError:
                read_as = "latin-1"
        head = head_fields(raw.decode(read_as, errors="replace"))
        text = build_ack(err.ack_code, head=head, issues=[err.issue], receiving_app=self.cfg.receiving_application,
                         receiving_facility=self.cfg.receiving_facility)
        mt = head.get("9.2", "")
        return Result(text, err.ack_code, f"?^{mt}" if mt else "", head.get("10", ""), [], [err.issue], charset=codec or "utf-8")


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


def _merge_numbers(e: dict, cur: dict, query: str) -> None:
    """A PUT replaces the whole resource, so numbers other systems sent earlier would vanish: keep them (and an
    order's authoredOn, which only the NW sets). A create-if-absent that matched an order lacking some of this
    message's numbers becomes a PUT of the stored resource with the numbers added, and nothing else changed."""
    res, req = e["resource"], e["request"]
    have = {(i.get("system"), i.get("value")) for i in res.get("identifier", [])}
    stored = {(i.get("system"), i.get("value")) for i in cur.get("identifier", [])}
    if req["method"] == "PUT":
        res["identifier"] = res.get("identifier", []) + [i for i in cur.get("identifier", []) if (i.get("system"), i.get("value")) not in have]
        if res.get("resourceType") == "ServiceRequest" and "authoredOn" not in res and "authoredOn" in cur:
            res["authoredOn"] = cur["authoredOn"]
    elif have - stored:
        body = {k: v for k, v in cur.items() if k not in ("id", "meta")}
        body["meta"] = res.get("meta", {})
        body["identifier"] = cur.get("identifier", []) + [i for i in res.get("identifier", []) if (i.get("system"), i.get("value")) not in stored]
        e["resource"], e["request"] = body, {"method": "PUT", "url": query}


def _id_type(ident: dict) -> str | None:
    return ((ident.get("type") or {}).get("coding") or [{}])[0].get("code")


def _conflicting_number(incoming: list[dict], stored: list[dict]) -> str | None:
    """Same identifier type (PLAC/FILL/ACSN) and system, different value: two different orders."""
    have = {(_id_type(i), i.get("system")): i.get("value") for i in stored if _id_type(i)}
    for i in incoming:
        k = (_id_type(i), i.get("system"))
        if k in have and have[k] != i.get("value"):
            return f"{k[0]} {have[k]}, this message {i.get('value')}"
    return None


def _older(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return datetime.fromisoformat(a) < datetime.fromisoformat(b)
    except (TypeError, ValueError):
        return False


def _stale(rt: str, req: dict, res: dict, cur: dict) -> str | None:
    """Why this entry would undo newer state on the server, or None."""
    if rt == "DiagnosticReport" and req["method"] == "PUT":
        new, old = T.REPORT_STATUS_RANK.get(res.get("status")), T.REPORT_STATUS_RANK.get(cur.get("status"))
        if new is None or old is None:
            return None
        if new < old:
            return f"late {res['status']} report ignored: the report is already {cur['status']}"
        if new == old and _older(res.get("issued"), cur.get("issued")):
            return f"older {res['status']} report ignored: the stored one was issued later ({cur['issued']})"
        return None
    if rt == "ServiceRequest":
        if req["method"] == "PUT":
            new = res.get("status")
        else:
            new = next((p.get("valueCode") for op in res.get("parameter", []) for p in op.get("part", [])
                        if p.get("name") == "value" and any(q.get("valueString") == "ServiceRequest.status" for q in op.get("part", []))), None)
        exits = T.ORDER_STATUS_EXITS.get(cur.get("status"))
        if new and exits is not None and new != cur.get("status") and new not in exits:
            return f"late order status {new} ignored: the order is already {cur['status']}"
    return None


def _drop_entry(bundle: dict, e: dict) -> None:
    """Remove one entry and its Provenance target; a Provenance left with no target (target is 1..*) goes too."""
    bundle["entry"].remove(e)
    patch_tok = e["request"]["url"].split("identifier=", 1)[-1].split(",")[0] if e["request"]["method"] == "PATCH" else None
    for other in list(bundle["entry"]):
        prov = other["resource"]
        if prov.get("resourceType") != "Provenance":
            continue
        prov["target"] = [t for t in prov["target"] if t.get("reference") != e["fullUrl"] and not (
            patch_tok and t.get("identifier") and token(t["identifier"].get("system"), t["identifier"]["value"]) == patch_tok)]
        if not prov["target"]:
            bundle["entry"].remove(other)
