"""Build HL7 v2 acknowledgements (original acknowledgement mode).

  MSH  sender and receiver swapped, fresh control id, same version, processing id and charset
  MSA  MSA-1 AA (accepted) / AE (error) / AR (reject), MSA-2 = the inbound MSH-10, MSA-3 short text
  ERR  one per issue. The ERR layout changed in v2.5:
         v2.3 / v2.4   ERR-1  <segment>^<sequence>^<field>^<code>&<text>&HL70357
         v2.5+         ERR-2  <segment>^<sequence>^<field>^^<component>
                       ERR-3  <code>^<text>^HL70357     ERR-4 severity E/W/I     ERR-8 user message
       Warnings on an AA are only sent for v2.5+, where ERR-4 can say "W"; a v2.3 receiver would
       read any ERR as an error.

The ACK is written with the inbound message's delimiters, because the sender parses it with its own.
If the inbound message could not be parsed at all, `head_fields` recovers what it can of MSH so the
NAK still carries the right MSA-2.
"""
from __future__ import annotations

import itertools
import threading
from datetime import datetime

from ..errors import ERROR_TEXT, Issue
from .charset import strip_bom
from .escape import escape
from .parser import SEGMENT_ID_RX, Delimiters, Message

_counter = itertools.count(1)
_lock = threading.Lock()


def _new_control_id() -> str:
    with _lock:
        n = next(_counter)
    return f"ACK{datetime.now().strftime('%y%m%d%H%M%S')}{n:05d}"[:20]


def _ts(now: datetime | None) -> str:
    now = (now or datetime.now().astimezone())
    return now.strftime("%Y%m%d%H%M%S") + (now.strftime("%z") if now.tzinfo else "")


def head_fields(raw: bytes | str) -> dict[str, str]:
    """Best-effort MSH fields from bytes that may not parse. Never raises."""
    try:
        text = strip_bom(raw).decode("latin-1") if isinstance(raw, bytes) else raw
        text = text.lstrip("\ufeff\x0b").removeprefix("\xef\xbb\xbf")
        line = text.replace("\r\n", "\r").replace("\n", "\r").split("\r", 1)[0]
        if not line.startswith("MSH") or len(line) < 8:
            return {}
        fs = line[3]
        if not ("!" <= fs <= "~") or fs.isalnum():
            return {}
        parts = line.split(fs)
        enc = parts[1] if len(parts) > 1 else ""
        comp = enc[0] if enc else "^"
        good_enc = 2 <= len(enc) <= 5 and len(set(enc + fs)) == len(enc) + 1 and all("!" <= c <= "~" and not c.isalnum() for c in enc)
        out = {"fs": fs if good_enc else "|", "enc": enc if good_enc else ""}
        for n in (3, 4, 5, 6, 10, 11, 12, 18):
            if n - 1 < len(parts):
                if n in (12, 18):
                    out[str(n)] = parts[n - 1].split(comp)[0][:40]
                else:                                     # echoed exactly as sent (still encoded)
                    out[str(n)] = parts[n - 1][:199 if n in (10, 11) else 180]
        if len(parts) > 8:
            out["9.2"] = (parts[8].split(comp) + ["", ""])[1][:3]
        return out
    except Exception:  # noqa: BLE001 -- best effort by design
        return {}


def build_ack(code: str, *, msg: Message | None = None, head: dict[str, str] | None = None, issues: list[Issue] | None = None,
              text: str = "", receiving_app: str = "V2FHIR", receiving_facility: str = "BRIDGE", now: datetime | None = None,
              default_version: str = "2.5.1") -> str:
    """Return the ACK as text with CR segment terminators (not MLLP framed)."""
    issues = issues or []
    if msg is not None:
        d = msg.delimiters
        m = msg.msh
        send_app, send_fac = m.raw(3), m.raw(4)
        version = msg.version or default_version
        control, processing, trigger, charset = m.raw(10).rstrip(), m.raw(11) or "P", msg.trigger, m.raw(18)
        rcv_app, rcv_fac = m.raw(5) or receiving_app, m.raw(6) or receiving_facility
    else:
        head = head or {}
        enc = head.get("enc") or "^~\\&"
        d = Delimiters(field=head.get("fs", "|"), component=enc[0], repetition=enc[1] if len(enc) > 1 else None,
                       escape=enc[2] if len(enc) > 2 else None, subcomponent=enc[3] if len(enc) > 3 else None, raw=enc)
        send_app, send_fac = head.get("3", ""), head.get("4", "")
        rcv_app, rcv_fac = head.get("5") or receiving_app, head.get("6") or receiving_facility
        version = head.get("12") or default_version
        control, processing, trigger, charset = head.get("10", ""), head.get("11") or "P", head.get("9.2", ""), head.get("18", "")
        if not _looks_like_version(version):
            version = default_version
    fs, cs = d.field, d.component

    def esc(s: str) -> str:
        return escape(s, field=d.field, component=d.component, repetition=d.repetition or "~", escape=d.escape,
                      subcomponent=d.subcomponent or "&")

    v25 = _version_tuple(version) >= (2, 5)
    msg_type = f"ACK{cs}{esc(trigger)}" + (f"{cs}ACK" if _version_tuple(version) >= (2, 3, 1) else "")
    # MSH-10 and MSH-11 are echoed exactly as the sender encoded them (MSA-2 must equal MSH-10 byte for byte)
    msh = ["MSH", d.raw, rcv_app, rcv_fac, send_app, send_fac, _ts(now), "", msg_type, _new_control_id(), processing, esc(version)]
    if charset:
        msh += [""] * 5 + [charset]
    errors = [i for i in issues if i.severity == "E"]
    msa_text = text or (str(errors[0]) if errors else "")
    segs = [fs.join(msh), fs.join(["MSA", code, control] + ([esc(msa_text[:80])] if msa_text and code != "AA" else []))]
    for issue in issues:
        if issue.severity != "E" and not v25:
            continue
        segs.append(_err(issue, v25, d, esc))
    return _no_controls("\r".join(segs)) + "\r"


def _no_controls(text: str) -> str:
    """Drop C0 control characters echoed from the inbound message (MSH-3..6, MSH-10, error text).
    A 0x0B or 0x1C inside an ACK would corrupt the sender's MLLP framing; CR only separates segments."""
    return "".join(c for c in text if c >= " " or c == "\r")


def _err(issue: Issue, v25: bool, d: Delimiters, esc) -> str:
    fs, cs, ss = d.field, d.component, d.subcomponent or d.component
    loc = issue.location
    seg = loc.segment if loc and SEGMENT_ID_RX.match(loc.segment or "") else ""
    seq = str(loc.sequence) if loc and loc.segment else ""
    fld = str(loc.field) if loc and loc.field is not None else ""
    comp = str(loc.component) if loc and loc.component is not None else ""
    label = ERROR_TEXT.get(issue.code, "Error")
    if v25:
        erl = cs.join([seg, seq, fld, "", comp]).rstrip(cs) if seg else ""
        return fs.join(["ERR", "", erl, cs.join([issue.code, esc(label), "HL70357"]), issue.severity, "", "", "", esc(issue.text[:250])])
    eld = cs.join([seg, seq, fld, ss.join([issue.code, esc(issue.text[:80]), "HL70357"])])
    return fs.join(["ERR", eld])


def _looks_like_version(v: str) -> bool:
    return bool(v) and all(p.isdigit() for p in v.split(".")) and len(v) <= 8


def _version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in v.split("."))
    except ValueError:
        return (2, 5, 1)
