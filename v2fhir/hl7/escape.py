"""HL7 v2 escape sequences (v2.5.1 chapter 2.7).

Delimiters inside data travel as escape sequences, with the escape character (usually "\\") on both sides:

  \\F\\ field sep   \\S\\ component sep   \\T\\ subcomponent sep   \\R\\ repetition sep   \\E\\ escape char
  \\Xhhhh\\  raw bytes in hex, decoded with the message character set (MSH-18)
  \\.br\\    line break (formatted text, FT). Radiology reports are full of these.
  \\H\\ \\N\\  start/stop highlighting: dropped (FHIR plain text has no highlighting)
  \\.sp n\\  n line breaks;  \\.in \\.ti \\.sk \\.ce \\.fi \\.nf formatting commands: dropped
  \\Cxxyy\\ \\Mxxyyzz\\  character-set switching (ISO 2022): dropped, see docs/MAPPING.md

Splitting on delimiters always happens on the raw text *before* unescaping, so an escaped "|" can never
split a field. An escape sequence that is never closed, or one we do not recognise, is kept literally:
losing characters from a report is worse than showing a stray backslash.
"""
from __future__ import annotations

import re

_SPACE_RX = re.compile(r"^\.sp\s*(\d*)$")


def unescape(text: str, *, field: str, component: str, repetition: str, escape: str | None, subcomponent: str,
             charset: str = "utf-8") -> str:
    if not escape or escape not in text:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch != escape:
            out.append(ch)
            i += 1
            continue
        end = text.find(escape, i + 1)
        if end < 0:                      # unterminated: keep the rest literally
            out.append(text[i:])
            break
        seq = text[i + 1:end]
        rep = _translate(seq, field, component, repetition, escape, subcomponent, charset)
        if rep is None:                  # unknown sequence: keep the escape char literally and move on
            out.append(ch)
            i += 1
            continue
        out.append(rep)
        i = end + 1
    return "".join(out)


def _translate(seq: str, fs: str, cs: str, rs: str, es: str, ss: str, charset: str) -> str | None:
    simple = {"F": fs, "S": cs, "T": ss, "R": rs, "E": es, "H": "", "N": ""}
    if seq in simple:
        return simple[seq]
    if seq == ".br":
        return "\n"
    m = _SPACE_RX.match(seq)
    if m:
        return "\n" * (int(m.group(1)) if m.group(1) else 1)
    if seq[:3] in (".in", ".ti", ".sk") or seq in (".ce", ".fi", ".nf"):
        return ""
    if seq[:1] == "X" and len(seq) > 1 and len(seq) % 2 == 1:
        try:
            raw = bytes.fromhex(seq[1:])
        except ValueError:
            return None
        return raw.decode(_codec(charset), errors="replace")
    if seq[:1] in ("C", "M") and len(seq) in (5, 7) and all(c in "0123456789ABCDEFabcdef" for c in seq[1:]):
        return ""
    return None


def _codec(charset: str) -> str:
    try:
        "".encode(charset)
        return charset
    except LookupError:
        return "latin-1"


def escape(text: str, *, field: str = "|", component: str = "^", repetition: str = "~", escape: str | None = "\\",
           subcomponent: str = "&") -> str:
    """Escape free text for an outgoing field (ACK MSA-3 / ERR-8). Line breaks become \\.br\\."""
    if escape:
        text = text.replace(escape, f"{escape}E{escape}")
        for ch, code in ((field, "F"), (component, "S"), (subcomponent, "T"), (repetition, "R")):
            text = text.replace(ch, f"{escape}{code}{escape}")
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", f"{escape}.br{escape}")
    else:
        for ch in (field, component, subcomponent, repetition, "\r", "\n"):
            text = text.replace(ch, " ")
    return text
