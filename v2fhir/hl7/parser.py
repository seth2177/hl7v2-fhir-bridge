"""HL7 v2 (ER7, "pipe and hat") parser.

Structure:  message -> segments -> fields -> repetitions -> components -> subcomponents.
The delimiters are declared by the message itself in MSH-1 (field separator) and MSH-2 (component,
repetition, escape, subcomponent), so nothing here assumes "|^~\\&".

Numbering follows the standard: fields are 1-based, and in MSH the field separator itself *is* MSH-1,
so MSH-2 is the encoding characters and MSH-9 is the message type -- the classic off-by-one.

Values are kept raw (still escaped) and only unescaped when read, so provenance can keep the exact
original text and an escaped delimiter can never split a field.

Null vs empty: an empty field means "no information sent"; the two-character value `""` is the HL7
explicit null, "delete whatever you have". Both read as None; `Segment.is_null()` tells them apart.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..errors import DATA_TYPE_ERROR, REQUIRED_FIELD_MISSING, SEGMENT_SEQUENCE_ERROR, Location, ParseError
from . import charset as _charset
from .escape import unescape

SEGMENT_ID_RX = re.compile(r"^[A-Z][A-Z0-9]{2}$")
HL7_NULL = '""'


@dataclass(frozen=True)
class Delimiters:
    """MSH-1 and MSH-2. A sender may omit trailing encoding characters (e.g. MSH-2 = "^~"): those are None."""
    field: str = "|"
    component: str = "^"
    repetition: str | None = "~"
    escape: str | None = "\\"
    subcomponent: str | None = "&"
    raw: str = "^~\\&"                 # MSH-2 exactly as received (v2.7+ may add a 5th, truncation char)


class Rep:
    """One repetition of a field: components, each a list of subcomponents (raw)."""
    __slots__ = ("raw", "_comps", "_msg")

    def __init__(self, raw: str, msg: Message):
        self.raw = raw
        self._msg = msg
        d = msg.delimiters
        sub = d.subcomponent
        self._comps = [(c.split(sub) if sub else [c]) for c in raw.split(d.component)] if raw else []

    def get(self, comp: int = 1, sub: int = 1) -> str | None:
        """Unescaped, stripped value, or None if empty / explicit null."""
        if comp < 1 or sub < 1 or comp > len(self._comps) or sub > len(self._comps[comp - 1]):
            return None
        raw = self._comps[comp - 1][sub - 1]
        if raw == "" or raw == HL7_NULL:
            return None
        value = self._msg.unescape(raw).strip()
        return value or None

    def component_raw(self, comp: int) -> str:
        if comp < 1 or comp > len(self._comps):
            return ""
        return (self._msg.delimiters.subcomponent or "").join(self._comps[comp - 1])

    def is_empty(self) -> bool:
        return not any(s and s != HL7_NULL for c in self._comps for s in c)

    def __repr__(self) -> str:
        return f"Rep({self.raw!r})"


class Segment:
    __slots__ = ("name", "fields", "_msg", "index", "sequence")

    def __init__(self, name: str, fields: list[str], msg: Message, index: int, sequence: int):
        self.name = name
        self.fields = fields          # fields[0] is the segment id; fields[n] is field n (raw)
        self._msg = msg
        self.index = index            # position in the message
        self.sequence = sequence      # 1-based occurrence of this segment id

    def raw(self, n: int) -> str:
        return self.fields[n] if 0 <= n < len(self.fields) else ""

    def is_null(self, n: int) -> bool:
        return self.raw(n) == HL7_NULL

    def reps(self, n: int) -> list[Rep]:
        raw = self.raw(n)
        if not raw or raw == HL7_NULL:
            return []
        if self.name == "MSH" and n in (1, 2):
            return [Rep(raw.replace(self._msg.delimiters.component, ""), self._msg)] if n == 1 else []
        rs = self._msg.delimiters.repetition
        return [r for r in (Rep(x, self._msg) for x in (raw.split(rs) if rs else [raw])) if not r.is_empty()]

    def rep(self, n: int, i: int = 1) -> Rep:
        reps = self.reps(n)
        return reps[i - 1] if 0 < i <= len(reps) else Rep("", self._msg)

    def get(self, n: int, comp: int = 1, sub: int = 1, rep: int = 1) -> str | None:
        return self.rep(n, rep).get(comp, sub)

    def loc(self, n: int | None = None, comp: int | None = None) -> Location:
        return Location(self.name, self.sequence, n, comp)

    def __str__(self) -> str:
        return self._msg.delimiters.field.join(self.fields) if self.name != "MSH" else \
            "MSH" + self.fields[1] + self.fields[2] + "".join(self._msg.delimiters.field + f for f in self.fields[3:])

    def __repr__(self) -> str:
        return f"Segment({self.name}#{self.sequence})"


@dataclass
class Message:
    text: str
    delimiters: Delimiters
    charset: str = "utf-8"
    segments: list[Segment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ---- access -------------------------------------------------------------
    def seg(self, name: str) -> Segment | None:
        return next((s for s in self.segments if s.name == name), None)

    def all(self, name: str) -> list[Segment]:
        return [s for s in self.segments if s.name == name]

    @property
    def msh(self) -> Segment:
        return self.segments[0]

    @property
    def message_type(self) -> str:
        return self.msh.get(9, 1) or ""

    @property
    def trigger(self) -> str:
        return self.msh.get(9, 2) or ""

    @property
    def structure(self) -> str:
        return self.msh.get(9, 3) or ""

    @property
    def type_label(self) -> str:
        return f"{self.message_type}^{self.trigger}"

    @property
    def control_id(self) -> str:
        return self.msh.get(10) or ""

    @property
    def version(self) -> str:
        return self.msh.get(12) or ""

    @property
    def sending_app(self) -> str:
        return self.msh.get(3) or ""

    @property
    def sending_facility(self) -> str:
        return self.msh.get(4) or ""

    @property
    def z_segments(self) -> list[Segment]:
        return [s for s in self.segments if s.name.startswith("Z")]

    def unescape(self, raw: str) -> str:
        d = self.delimiters
        return unescape(raw, field=d.field, component=d.component, repetition=d.repetition or "", escape=d.escape,
                        subcomponent=d.subcomponent or "", charset=self.charset)


def parse_bytes(raw: bytes, default_charset: str = "utf-8", fallback_charsets: tuple[str, ...] = ("cp1252", "latin-1")) -> Message:
    text, codec, warnings = _charset.decode(raw, default_charset, fallback_charsets)
    msg = parse(text, charset=codec)
    msg.warnings[:0] = warnings
    return msg


def parse(text: str, charset: str = "utf-8") -> Message:
    text = text.lstrip("\ufeff").strip("\x00")
    lines = _split_segments(text)
    if not lines:
        raise ParseError("empty message", SEGMENT_SEQUENCE_ERROR)
    head = lines[0]
    if not head.startswith("MSH"):
        raise ParseError(f"not an HL7 v2 message: must start with MSH, found {head[:3]!r}", SEGMENT_SEQUENCE_ERROR)
    delims = _delimiters(head)
    msg = Message(text=text, delimiters=delims, charset=charset)
    counts: dict[str, int] = {}
    fs = delims.field
    for idx, line in enumerate(lines):
        name = line[:3]
        if not SEGMENT_ID_RX.match(name) or (len(line) > 3 and line[3] != fs):
            # no Location: the "segment id" is garbage and must not be echoed into ERR-2
            raise ParseError(f"segment {idx + 1} has an invalid segment id {line[:8]!r}", SEGMENT_SEQUENCE_ERROR)
        if name == "MSH" and idx > 0:
            raise ParseError("a second MSH inside one message (batch files must be split first)", SEGMENT_SEQUENCE_ERROR, Location("MSH", 2))
        counts[name] = counts.get(name, 0) + 1
        if name == "MSH":
            enc = delims.raw
            rest = line[4 + len(enc):]
            fields = ["MSH", fs, enc] + (rest.split(fs)[1:] if rest else [])
        else:
            fields = line.split(fs)
        msg.segments.append(Segment(name, fields, msg, idx, counts[name]))
    _check_msh(msg)
    return msg


def _split_segments(text: str) -> list[str]:
    # The standard terminator is CR. Many senders use CRLF, some bare LF (files, Unix tools).
    # If the message contains any CR, CR is the terminator and a bare LF is data (a line break a
    # reporting system left inside report text), so it must not start a bogus "segment".
    if "\r" in text:
        parts = text.replace("\r\n", "\r").split("\r")
    else:
        parts = text.split("\n")
    return [p for p in parts if p.strip()]


def _usable_delimiter(c: str) -> bool:
    """Printable ASCII punctuation only: never a letter, digit, space or control character (a 0x0B/0x1C
    delimiter would be echoed into the ACK and break MLLP framing)."""
    return len(c) == 1 and "!" <= c <= "~" and not c.isalnum()


def _delimiters(head: str) -> Delimiters:
    if len(head) < 5:
        raise ParseError("MSH is truncated before the encoding characters", REQUIRED_FIELD_MISSING, Location("MSH", 1, 2))
    fs = head[3]
    end = head.find(fs, 4)
    enc = head[4:end] if end >= 0 else head[4:]
    if not enc or not _usable_delimiter(fs):
        raise ParseError(f"invalid field separator {fs!r} / encoding characters {enc!r}", REQUIRED_FIELD_MISSING, Location("MSH", 1, 2))
    comp = enc[0]
    rep = enc[1] if len(enc) > 1 else None
    esc = enc[2] if len(enc) > 2 else None
    sub = enc[3] if len(enc) > 3 else None
    chars = [c for c in (fs, comp, rep, esc, sub) if c is not None]
    if len(set(chars)) != len(chars) or not all(_usable_delimiter(c) for c in chars) or len(enc) > 5:
        raise ParseError(f"invalid encoding characters {enc!r}", DATA_TYPE_ERROR, Location("MSH", 1, 2))
    return Delimiters(field=fs, component=comp, repetition=rep, escape=esc, subcomponent=sub, raw=enc)


def _check_msh(msg: Message) -> None:
    msh = msg.msh
    for n, what in ((9, "message type"), (10, "message control id"), (12, "version id")):
        if not msh.get(n):
            raise ParseError(f"MSH-{n} ({what}) is empty", REQUIRED_FIELD_MISSING, Location("MSH", 1, n))
    if not msh.get(9, 2) and msh.get(9) != "ACK":
        raise ParseError("MSH-9.2 (trigger event) is empty", REQUIRED_FIELD_MISSING, Location("MSH", 1, 9, 2))


def split_batch(text: str) -> list[str]:
    """Split a file holding several messages (and optional FHS/BHS/BTS/FTS wrappers) into messages."""
    lines = _split_segments(text.lstrip("\ufeff"))
    out: list[list[str]] = []
    for line in lines:
        if line[:3] in ("FHS", "BHS", "BTS", "FTS"):
            continue
        if line.startswith("MSH") or not out:
            out.append([line])
        else:
            out[-1].append(line)
    return ["\r".join(m) + "\r" for m in out]


def split_batch_bytes(raw: bytes) -> list[bytes]:
    """split_batch on raw bytes. latin-1 maps every byte to one character and back, so each message's
    bytes are untouched and its own MSH-18 still decides how it is decoded."""
    return [m.encode("latin-1") for m in split_batch(_charset.strip_bom(raw).decode("latin-1"))]
