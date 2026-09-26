"""Turn MLLP payload bytes into text, honouring MSH-18 (character set, HL7 table 0211).

HL7 v2 is a byte protocol: nothing in the frame says how names are encoded except MSH-18, and most
senders leave it empty. The rules here:

  * MSH-18 declared and known  -> decode strictly. If the bytes are not valid in that character set,
                                  refuse with AE (CharsetError). Guessing would store a corrupted name
                                  on a real patient; the sender has to fix its configuration.
  * MSH-18 empty or unknown     -> try UTF-8, then each fallback (default cp1252, then latin-1, which
                                  can decode any byte) and add a warning naming the one that worked.

MSH itself is always ASCII, so it can be read before the character set is known by decoding as
latin-1 (a lossless 1:1 byte mapping). Only ASCII-compatible encodings are supported (not UTF-16).
"""
from __future__ import annotations

from .._util import first_line
from ..errors import DATA_TYPE_ERROR, CharsetError, Location

# HL7 table 0211 -> Python codec. Includes the common non-standard spellings seen in the field.
CHARSETS = {
    "ASCII": "ascii",
    "8859/1": "latin-1",
    "8859/2": "iso8859-2",
    "8859/3": "iso8859-3",
    "8859/4": "iso8859-4",
    "8859/5": "iso8859-5",
    "8859/6": "iso8859-6",
    "8859/7": "iso8859-7",
    "8859/8": "iso8859-8",
    "8859/9": "iso8859-9",
    "8859/15": "iso8859-15",
    "UNICODE UTF-8": "utf-8",
    "GB 18030-2000": "gb18030",
    "KS X 1001": "euc-kr",
    "BIG-5": "big5",
    "ISO IR6": "ascii",
    "ISO IR100": "latin-1",
    # non-standard but common
    "UTF-8": "utf-8",
    "UTF8": "utf-8",
    "UNICODE": "utf-8",
    "ISO-8859-1": "latin-1",
    "LATIN1": "latin-1",
    "WINDOWS-1252": "cp1252",
    "CP1252": "cp1252",
}

UTF8_BOM = b"\xef\xbb\xbf"


def declared_charset(raw: bytes) -> str:
    """MSH-18 (first repetition), read from the bytes before decoding. '' if absent."""
    head = first_line(raw.decode("latin-1"))
    if not head.startswith("MSH") or len(head) < 5:
        return ""
    fs = head[3]
    parts = head.split(fs)
    if len(parts) < 18:
        return ""
    enc = parts[1]
    rep = enc[1] if len(enc) > 1 else "~"
    return parts[17].split(rep)[0].strip()


def decode(raw: bytes, default: str = "utf-8", fallbacks: tuple[str, ...] = ("cp1252", "latin-1")) -> tuple[str, str, list[str]]:
    """Returns (text, codec used, warnings)."""
    warnings: list[str] = []
    if raw.startswith(UTF8_BOM):
        raw = raw[len(UTF8_BOM):]
    declared = declared_charset(raw)
    codec = CHARSETS.get(declared.upper()) if declared else None
    if declared and not codec:
        warnings.append(f"MSH-18 '{declared}' is not a known character set; decoding by detection")
    if codec:
        try:
            return raw.decode(codec), codec, warnings
        except UnicodeDecodeError as e:
            raise CharsetError(f"MSH-18 declares {declared} but byte 0x{raw[e.start]:02X} at offset {e.start} is not valid {declared}",
                               DATA_TYPE_ERROR, Location("MSH", 1, 18)) from None
    for i, c in enumerate((default, *fallbacks)):
        try:
            text = raw.decode(c)
        except UnicodeDecodeError:
            continue
        if i > 0 and any(b > 0x7F for b in raw):
            warnings.append(f"MSH-18 empty and message is not valid {default}; decoded as {c}")
        return text, c, warnings
    return raw.decode("latin-1"), "latin-1", warnings   # unreachable: latin-1 decodes anything
