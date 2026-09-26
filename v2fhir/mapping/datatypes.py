"""v2 data types -> FHIR data types: TS/DTM, CX, EI, HD, XPN, XCN, CNN, XAD, XTN, CE/CWE.

All functions take a parsed `Rep` (one field repetition) and return plain FHIR JSON (dicts), or None
when there is nothing to map. Anything dropped or assumed is reported through `ctx.warn`.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import quote

from ..errors import Location
from ..hl7.parser import Rep
from . import tables as T
from .context import Ctx

# ---- timestamps -------------------------------------------------------------------------------
# v2 TS/DTM: YYYY[MM[DD[HH[MM[SS[.S[S[S[S]]]]]]]]][+/-ZZZZ]. Any precision is legal.
# FHIR date:     YYYY | YYYY-MM | YYYY-MM-DD
# FHIR dateTime: a date, or YYYY-MM-DDThh:mm:ss[.fff](Z|+hh:mm). If there is a time there MUST be an offset.
# FHIR instant:  always full to the second, always an offset.
TS_RX = re.compile(r"^(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\.\d{1,4})?([+-]\d{4})?$")


def ts(value: str | None, kind: str, ctx: Ctx, where: Location | None = None) -> str | None:
    """Convert a v2 timestamp. kind: 'date' | 'dateTime' | 'instant'."""
    if not value:
        return None
    m = TS_RX.match(value.strip())
    if not m:
        ctx.warn(f"unparseable timestamp {value!r} dropped", where)
        return None
    y, mo, d, hh, mi, ss, frac, off = m.groups()
    try:  # range check with the real calendar (Feb 30 is not a date)
        datetime(int(y), int(mo or 1), int(d or 1), int(hh or 0), int(mi or 0), int(ss or 0))
        if y == "0000":
            raise ValueError
    except ValueError:
        ctx.warn(f"invalid timestamp {value!r} dropped", where)
        return None
    date = y + (f"-{mo}" if mo else "") + (f"-{d}" if mo and d else "")
    if kind == "date" or hh is None or d is None:
        if kind == "instant":
            ctx.warn(f"timestamp {value!r} has no time; an instant needs one, dropped", where)
            return None
        return date
    time = f"{hh}:{mi or '00'}:{ss or '00'}" + (frac or "")
    if off:
        offset = f"{off[:3]}:{off[3:]}"
        if int(off[1:3]) > 14 or int(off[3:]) > 59:
            ctx.warn(f"timestamp {value!r} has an impossible UTC offset, dropped", where)
            return None
    else:
        tz = ctx.tz
        if tz is None:
            ctx.warn(f"timestamp {value!r} has no UTC offset and no default time zone is configured; kept as date only", where)
            return None if kind == "instant" else date
        local = datetime(int(y), int(mo), int(d), int(hh), int(mi or 0), int(ss or 0), tzinfo=tz)
        delta = local.utcoffset() or timedelta(0)
        minutes = int(delta.total_seconds() // 60)
        sign = "+" if minutes >= 0 else "-"
        offset = f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
        ctx.note_assumed_tz(value, where)
    return f"{date}T{time}{offset}"


# ---- identifiers --------------------------------------------------------------------------------
def hd_system(namespace: str | None, universal: str | None, universal_type: str | None, ctx: Ctx,
              where: Location | None = None) -> str | None:
    """HD (assigning authority / namespace) -> Identifier.system.

    Order: site config for the namespace or the universal id, then an ISO OID / UUID / URI universal id, then
    <identifier_system_base><namespace>, then <identifier_system_base><universal id> (with a warning). None only
    when the HD is empty. An identifier without a system must never become a match key: in FHIR,
    identifier=1001 matches 1001 in *any* system (R4 search.html#token).
    """
    cfg = ctx.cfg
    for key in (namespace, universal):
        if key and key in cfg.assigning_authorities:
            return cfg.assigning_authorities[key]
    utype = (universal_type or "").upper()
    if universal and utype == "ISO":
        if re.fullmatch(r"[0-2](\.(0|[1-9]\d*))+", universal):
            return f"urn:oid:{universal}"
        ctx.warn(f"assigning authority {universal!r} is not a valid ISO OID", where)
    if universal and utype == "UUID":
        return f"urn:uuid:{universal.lower()}"
    if universal and utype in ("URI", "URL"):
        return universal
    if namespace:
        return cfg.identifier_system_base + quote(namespace.lower(), safe="-._~")
    if universal:
        system = cfg.identifier_system_base + quote(universal.lower(), safe="-._~")
        ctx.warn(f"assigning authority {universal!r} (type {utype or 'none'}) has no configured system; using {system}", where)
        return system
    return None


def identifier_type(code: str | None) -> dict | None:
    if not code:
        return None
    coding = {"system": T.IDENTIFIER_TYPE_SYSTEM, "code": code}
    if code in T.IDENTIFIER_TYPE_DISPLAY:
        coding["display"] = T.IDENTIFIER_TYPE_DISPLAY[code]
    return {"coding": [coding]}


def cx(rep: Rep, ctx: Ctx, where: Location | None = None, default_authority: str | None = None) -> dict | None:
    """CX: ID ^ check digit ^ scheme ^ assigning authority (HD) ^ identifier type ^ assigning facility."""
    value = rep.get(1)
    if not value:
        return None
    ns, uni, utype = rep.get(4, 1), rep.get(4, 2), rep.get(4, 3)
    if not (ns or uni) and default_authority:
        ns = default_authority
        ctx.warn(f"identifier {value!r} has no assigning authority; assumed {default_authority}", where)
    ident: dict = {}
    t = identifier_type(rep.get(5))
    if t:
        ident["type"] = t
    system = hd_system(ns, uni, utype, ctx, where)
    if system:
        ident["system"] = system
    ident["value"] = value
    if ns or uni:
        ident["assigner"] = {"display": ns or uni}
    return ident


def ei(value: str | None, ns: str | None, uni: str | None, utype: str | None, type_code: str, fallback_kind: str, ctx: Ctx) -> dict | None:
    """EI (entity identifier: placer/filler order number, accession) -> Identifier with a v2-0203 type.

    Without an assigning authority the system falls back to <identifier_system_base><fallback_kind>, which
    is site-wide on purpose: an ORU from the reporting system must produce the same key as the ORM from
    the RIS, so the system cannot depend on who sent the message.
    """
    if not value:
        return None
    system = hd_system(ns, uni, utype, ctx) or ctx.cfg.identifier_system_base + fallback_kind
    return {"type": identifier_type(type_code), "system": system, "value": value}


def ei_rep(rep: Rep, type_code: str, fallback_kind: str, ctx: Ctx) -> dict | None:
    return ei(rep.get(1), rep.get(2), rep.get(3), rep.get(4), type_code, fallback_kind, ctx)


# ---- names ---------------------------------------------------------------------------------------
def _human_name(family: str | None, given: list[str | None], suffix: list[str | None], prefix: str | None, use_code: str | None) -> dict | None:
    name: dict = {}
    use = T.NAME_USE_0200.get(use_code or "")
    if use:
        name["use"] = use
    if family:
        name["family"] = family
    g = [x for x in given if x]
    if g:
        name["given"] = g
    if prefix:
        name["prefix"] = [prefix]
    s = [x for x in suffix if x]
    if s:
        name["suffix"] = s
    if not (family or g):
        return None
    return name


def xpn(rep: Rep) -> dict | None:
    """XPN: family (FN: surname & ...) ^ given ^ middle ^ suffix ^ prefix ^ degree ^ name type."""
    return _human_name(rep.get(1, 1), [rep.get(2), rep.get(3)], [rep.get(4), rep.get(6)], rep.get(5), rep.get(7))


def xcn(rep: Rep, ctx: Ctx) -> tuple[dict | None, dict | None]:
    """XCN (a person with an id): id ^ family ^ given ^ middle ^ suffix ^ prefix ^ degree ^ src ^ authority(HD) ^ name type ... ^ id type(13).
    Returns (identifier, HumanName)."""
    name = _human_name(rep.get(2, 1), [rep.get(3), rep.get(4)], [rep.get(5), rep.get(7)], rep.get(6), rep.get(10))
    value = rep.get(1)
    ident = None
    if value:
        ident = {}
        t = identifier_type(rep.get(13))
        if t:
            ident["type"] = t
        ident["system"] = hd_system(rep.get(9, 1), rep.get(9, 2), rep.get(9, 3), ctx) or _provider_fallback(value, rep.get(13), ctx)
        ident["value"] = value
    return ident, name


def cnn_in_ndl(rep: Rep, ctx: Ctx) -> tuple[dict | None, dict | None]:
    """NDL (OBR-32 principal result interpreter): component 1 is a CNN whose parts are SUBcomponents:
    id & family & given & middle & suffix & prefix & degree & src & authority ns & universal id & universal type."""
    name = _human_name(rep.get(1, 2), [rep.get(1, 3), rep.get(1, 4)], [rep.get(1, 5), rep.get(1, 7)], rep.get(1, 6), None)
    value = rep.get(1, 1)
    ident = None
    if value:
        ident = {"system": hd_system(rep.get(1, 9), rep.get(1, 10), rep.get(1, 11), ctx) or _provider_fallback(value, None, ctx),
                 "value": value}
    return ident, name


def _provider_fallback(value: str, id_type: str | None, ctx: Ctx) -> str:
    """A provider id with no assigning authority: an NPI is the US NPI system; anything else shares one site-wide
    system, which only works if every sender numbers providers the same way, so say so."""
    if (id_type or "").upper() == "NPI":
        return ctx.cfg.assigning_authorities.get("NPI", "http://hl7.org/fhir/sid/us-npi")
    system = ctx.cfg.identifier_system_base + "provider"
    ctx.warn(f"provider id {value!r} has no assigning authority; matched on the site-wide {system}")
    return system


def name_text(name: dict | None) -> str:
    if not name:
        return ""
    return " ".join([*(name.get("prefix") or []), *(name.get("given") or []), name.get("family", ""), *(name.get("suffix") or [])]).strip()


# ---- address and telecom ------------------------------------------------------------------------
def xad(rep: Rep) -> dict | None:
    """XAD: street (SAD: street & ...) ^ other designation ^ city ^ state ^ zip ^ country ^ address type."""
    addr: dict = {}
    t = rep.get(7)
    if t in T.ADDRESS_USE_0190:
        addr["use"] = T.ADDRESS_USE_0190[t]
    if t in T.ADDRESS_TYPE_0190:
        addr["type"] = T.ADDRESS_TYPE_0190[t]
    lines = [x for x in (rep.get(1, 1), rep.get(2)) if x]
    if lines:
        addr["line"] = lines
    for comp, key in ((3, "city"), (4, "state"), (5, "postalCode"), (6, "country")):
        v = rep.get(comp)
        if v:
            addr[key] = v
    return addr if set(addr) - {"use", "type"} else None


def xtn(rep: Rep, default_use: str | None) -> dict | None:
    """XTN: [formatted number] ^ use (0201) ^ equipment (0202) ^ email ^ country ^ area ^ local ^ extension."""
    use_code, equip, email = rep.get(2), (rep.get(3) or "").upper(), rep.get(4)
    cp: dict = {}
    if email or use_code == "NET" or equip in ("INTERNET", "X.400"):
        value = email or rep.get(1)
        if not value:
            return None
        cp["system"], cp["value"] = "email", value
    else:
        value = rep.get(1)
        if not value and rep.get(7):
            area, local, ext = rep.get(6), rep.get(7), rep.get(8)
            value = (f"({area}) " if area else "") + local + (f" x{ext}" if ext else "")
        if not value:
            return None
        cp["system"], cp["value"] = (T.TELECOM_SYSTEM_0202.get(equip, "other") if equip else "phone"), value
    if equip == "CP":
        use = "mobile"
    elif use_code and use_code != "NET":
        use = T.TELECOM_USE_0201.get(use_code)          # an unknown use code is left out, not guessed
    else:
        use = default_use
    if use:
        cp["use"] = use
    return cp


# ---- coded values ---------------------------------------------------------------------------------
def coding_system(name: str | None, ctx: Ctx) -> str | None:
    if not name:
        return None
    return T.CODING_SYSTEM_0396.get(name.upper()) or ctx.cfg.code_system_base + quote(name.lower(), safe="-._~")


def ce(rep: Rep, ctx: Ctx) -> dict | None:
    """CE/CWE: code ^ text ^ system ^ alt code ^ alt text ^ alt system [^ ver ^ alt ver ^ original text]."""
    codings = []
    for c, t, s in ((1, 2, 3), (4, 5, 6)):
        code = rep.get(c)
        if not code:
            continue
        coding: dict = {}
        system = coding_system(rep.get(s), ctx)
        if system:
            coding["system"] = system
        coding["code"] = code
        if rep.get(t):
            coding["display"] = rep.get(t)
        codings.append(coding)
    text = rep.get(9) or rep.get(2) or rep.get(5)
    if not codings and not text:
        return None
    cc: dict = {}
    if codings:
        cc["coding"] = codings
    if text:
        cc["text"] = text
    return cc


def modality_coding(code: str | None) -> dict | None:
    m = T.MODALITY_FROM_0074.get((code or "").upper())
    if not m:
        return None
    return {"system": T.DICOM_DCM, "code": m, "display": T.DICOM_MODALITY_DISPLAY[m]}
