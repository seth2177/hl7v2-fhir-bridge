"""Escape sequences and v2 data type conversions (TS, CX/HD, XPN, XTN, XAD, CE)."""
from datetime import datetime, timezone

import pytest

from v2fhir.hl7.escape import escape, unescape
from v2fhir.hl7.parser import parse
from v2fhir.mapping import datatypes as dt
from v2fhir.mapping.context import Ctx

D = dict(field="|", component="^", repetition="~", escape="\\", subcomponent="&")


@pytest.mark.parametrize("raw, expected", [
    ("a\\F\\b", "a|b"), ("a\\S\\b", "a^b"), ("a\\T\\b", "a&b"), ("a\\R\\b", "a~b"), ("a\\E\\b", "a\\b"),
    ("x\\.br\\y", "x\ny"), ("x\\.sp2\\y", "x\n\ny"), ("\\H\\bold\\N\\", "bold"), ("\\.in+4\\x", "x"),
    ("caf\\XC3A9\\", "café"), ("\\X0D0A\\", "\r\n"), ("\\C2842\\abc", "abc"),
    ("C:\\temp\\file", "C:\\temp\\file"),        # unknown sequence: kept literally
    ("broken \\F", "broken \\F"),                   # unterminated: kept literally
    ("\\XZZ\\", "\\XZZ\\"),                         # bad hex: kept
    ("plain", "plain"),
])
def test_unescape(raw, expected):
    assert unescape(raw, **D) == expected


def test_hex_escape_uses_message_charset():
    assert unescape("\\XE9\\", **D, charset="latin-1") == "é"


def test_escape_round_trip():
    text = "a|b^c&d~e\\f\nline"
    assert unescape(escape(text), **D) == text
    assert "|" not in escape("a|b")


def test_escaped_delimiters_never_split_fields():
    m = parse("MSH|^~\\&|A|B|C|D|2026||ORU^R01|1|P|2.5\rOBX|1|ST|X||a\\F\\b\\S\\c\\R\\d||\r")
    obx = m.seg("OBX")
    assert obx.get(5) == "a|b^c~d" and len(obx.reps(5)) == 1 and obx.raw(6) == ""


# ---- timestamps -----------------------------------------------------------------------------------
def ctx(cfg, text="MSH|^~\\&|A|B|C|D|2026||ADT^A04|1|P|2.5\r"):
    return Ctx(msg=parse(text), cfg=cfg, now=datetime(2026, 1, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize("value, kind, expected", [
    ("2026", "dateTime", "2026"),
    ("202609", "dateTime", "2026-09"),
    ("20260915", "dateTime", "2026-09-15"),
    ("20260915", "date", "2026-09-15"),
    ("202609151430-0500", "dateTime", "2026-09-15T14:30:00-05:00"),     # minutes precision: seconds padded
    ("20260915143015.1234+0530", "dateTime", "2026-09-15T14:30:15.1234+05:30"),
    ("20260915143015+0000", "instant", "2026-09-15T14:30:15+00:00"),
    ("20260915143015-0500", "date", "2026-09-15"),
    ("19680412", "date", "1968-04-12"),
    ("196804", "date", "1968-04"),
])
def test_timestamps_with_offsets_and_partial_precision(cfg, value, kind, expected):
    assert dt.ts(value, kind, ctx(cfg)) == expected


def test_timestamp_without_offset_uses_site_zone_with_dst(cfg):
    c = ctx(cfg)
    assert dt.ts("20260115083000", "dateTime", c) == "2026-01-15T08:30:00-06:00"    # CST
    assert dt.ts("20260715083000", "dateTime", c) == "2026-07-15T08:30:00-05:00"    # CDT
    c.finish()
    assert "2 timestamp(s) without UTC offset" in c.warnings[0].text


def test_timestamp_without_offset_and_no_zone_keeps_only_the_date(cfg):
    cfg.default_timezone = ""
    c = ctx(cfg)
    assert dt.ts("20260115083000", "dateTime", c) == "2026-01-15"
    assert dt.ts("20260115083000", "instant", c) is None
    assert "no default time zone" in c.warnings[0].text


@pytest.mark.parametrize("bad", ["2026-09-15", "20261345", "20260230", "00000101", "2026091", "202609151430+2500", "yesterday"])
def test_invalid_timestamps_are_dropped_with_warning(cfg, bad):
    c = ctx(cfg)
    assert dt.ts(bad, "dateTime", c) is None
    assert c.warnings


def test_instant_needs_a_time(cfg):
    assert dt.ts("20260915", "instant", ctx(cfg)) is None


# ---- identifiers ------------------------------------------------------------------------------------
def rep(field_text: str):
    return parse("MSH|^~\\&|A|B|C|D|2026||ADT^A04|1|P|2.5\rPID|1||" + field_text + "\r").seg("PID").rep(3)


def test_cx_system_from_config_oid_uuid_uri_and_namespace(cfg):
    c = ctx(cfg)
    assert dt.cx(rep("1^^^SYNTH_HOSP^MR"), c)["system"] == "http://example.org/fhir/sid/synth-hosp"      # configured
    # an unconfigured namespace that also carries an OID: the OID is the globally unique one
    assert dt.cx(rep("1^^^X&2.16.840.1.113883.19&ISO^MR"), c)["system"] == "urn:oid:2.16.840.1.113883.19"
    assert dt.cx(rep("1^^^&2.16.840.1.113883.19&ISO^MR"), c)["system"] == "urn:oid:2.16.840.1.113883.19"
    assert dt.cx(rep("1^^^&6F9619FF-8B86-D011-B42D-00C04FC964FF&UUID"), c)["system"] == "urn:uuid:6f9619ff-8b86-d011-b42d-00c04fc964ff"
    assert dt.cx(rep("1^^^&https://id.example.org/mrn&URI"), c)["system"] == "https://id.example.org/mrn"
    assert dt.cx(rep("1^^^Some Clinic^MR"), c)["system"] == "http://example.org/fhir/sid/some%20clinic"
    ident = dt.cx(rep("42^^^SYNTH_HOSP^MR"), c)
    assert ident["type"]["coding"][0] == {"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MR", "display": "Medical record number"}


def test_cx_without_assigning_authority_uses_configured_default(cfg):
    c = ctx(cfg)
    ident = dt.cx(rep("777"), c, None, "SYNTH_HOSP")
    assert ident["system"] == "http://example.org/fhir/sid/synth-hosp"
    assert "no assigning authority" in c.warnings[0].text


def test_xpn_xtn_xad_ce(cfg):
    c = ctx(cfg)
    r = parse("MSH|^~\\&|A|B|C|D|2026||ADT^A04|1|P|2.5\rPID|1||x||DE LA CRUZ&DE LA&CRUZ^ANA^B^JR^DR^PHD^L\r").seg("PID").rep(5)
    assert dt.xpn(r) == {"use": "official", "family": "DE LA CRUZ", "given": ["ANA", "B"], "prefix": ["DR"], "suffix": ["JR", "PHD"]}
    t = parse("MSH|^~\\&|A|B|C|D|2026||ADT^A04|1|P|2.5\rPID|1||^PRN^PH^^1^210^5550100^12~^NET^Internet^a@example.org~^PRN^CP^^^210^5550199\r")
    phones = [dt.xtn(x, "home") for x in t.seg("PID").reps(3)]
    assert phones == [{"system": "phone", "value": "(210) 5550100 x12", "use": "home"},
                      {"system": "email", "value": "a@example.org", "use": "home"},
                      {"system": "phone", "value": "(210) 5550199", "use": "mobile"}]
    a = parse("MSH|^~\\&|A|B|C|D|2026||ADT^A04|1|P|2.5\rPID|1||1 Main&Main St^Unit 2^Town^TX^78000^USA^M\r").seg("PID").rep(3)
    assert dt.xad(a) == {"use": "home", "type": "postal", "line": ["1 Main", "Unit 2"], "city": "Town", "state": "TX", "postalCode": "78000", "country": "USA"}
    cc = dt.ce(rep("71250^CT CHEST^C4^CTCH^Chest CT^99RIS"), c)
    assert cc["coding"][0] == {"system": "http://www.ama-assn.org/go/cpt", "code": "71250", "display": "CT CHEST"}
    assert cc["coding"][1]["system"] == "http://example.org/fhir/CodeSystem/99ris" and cc["text"] == "CT CHEST"


def test_sp_line_count_is_bounded():
    """\\.sp n\\ (v2.5.1 2.7.6) is n line breaks. A 16-byte escape asking for 4 billion used to allocate
    gigabytes; 5,000 digits raised ValueError (int() digit limit) and became an internal error."""
    esc = dict(field="|", component="^", repetition="~", escape="\\", subcomponent="&")
    assert unescape("a\\.sp 4000000000\\b", **esc) == "a" + "\n" * 20 + "b"
    assert unescape("a\\.sp " + "9" * 5000 + "\\b", **esc) == "a" + "\n" * 20 + "b"
    assert unescape("a\\.sp 3\\b", **esc) == "a\n\n\nb"


def test_skip_and_centre_keep_words_and_lines_apart():
    """v2.5.1 2.7.6: \\.sk n\\ skips n spaces, \\.ce\\ ends the current line and centres the next. Dropping them
    glued "RIGHT" and "LOWER" together."""
    esc = dict(field="|", component="^", repetition="~", escape="\\", subcomponent="&")
    assert unescape("RIGHT\\.sk 1\\LOWER", **esc) == "RIGHT LOWER"
    assert unescape("FINDINGS\\.ce\\Normal", **esc) == "FINDINGS\nNormal"
    assert unescape("a\\.sk 99999\\b", **esc) == "a" + " " * 80 + "b"
