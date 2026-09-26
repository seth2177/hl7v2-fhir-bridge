"""HL7 v2 parsing: delimiters, numbering, repetitions, components, nulls, line endings, charsets."""
import pytest

from v2fhir.errors import CharsetError, ParseError
from v2fhir.hl7.charset import declared_charset, decode
from v2fhir.hl7.parser import parse, parse_bytes, split_batch, split_batch_bytes

MSH = "MSH|^~\\&|RIS|HOSP|V2FHIR|BRIDGE|20260101120000||ADT^A04^ADT_A01|CTRL1|P|2.5.1"


def test_msh_field_numbering():
    m = parse(MSH + "\r")
    assert m.msh.raw(1) == "|" and m.msh.raw(2) == "^~\\&"
    assert m.msh.get(3) == "RIS" and m.msh.get(9) == "ADT" and m.msh.get(9, 2) == "A04" and m.msh.get(9, 3) == "ADT_A01"
    assert (m.control_id, m.version, m.type_label, m.structure) == ("CTRL1", "2.5.1", "ADT^A04", "ADT_A01")


def test_repetitions_components_subcomponents():
    m = parse(MSH + "\rPID|1||A1^^^AUTH1&1.2.3&ISO^MR~B2^^^AUTH2^PI||FAM^GIV^MID\r")
    pid = m.seg("PID")
    reps = pid.reps(3)
    assert [r.get(1) for r in reps] == ["A1", "B2"]
    assert reps[0].get(4, 1) == "AUTH1" and reps[0].get(4, 2) == "1.2.3" and reps[0].get(4, 3) == "ISO"
    assert pid.get(3, rep=2, comp=5) == "PI"
    assert pid.get(5, 3) == "MID"
    assert pid.get(5, 9) is None and pid.get(99) is None


def test_empty_fields_trailing_separators_and_explicit_null():
    m = parse(MSH + "\rPID|1||123^^^H^MR||DOE^JANE^^^^||||\"\"|||||\r")
    pid = m.seg("PID")
    assert pid.get(5) == "DOE" and pid.get(5, 2) == "JANE" and pid.get(5, 6) is None
    assert pid.get(8) is None and not pid.is_null(8)       # empty: no information sent
    assert pid.get(9) is None and pid.is_null(9)           # "": explicit delete
    assert pid.reps(9) == [] and pid.get(30) is None


def test_values_are_stripped_of_padding():
    m = parse(MSH + "\rPID|1||123   ^^^H^MR||SMITH     ^JOHN   \r")
    assert m.seg("PID").get(3) == "123" and m.seg("PID").get(5) == "SMITH"


@pytest.mark.parametrize("eol", ["\r", "\r\n", "\n"])
def test_segment_terminators(eol):
    m = parse(eol.join([MSH, "EVN|A04", "PID|1||123^^^H^MR"]) + eol)
    assert [s.name for s in m.segments] == ["MSH", "EVN", "PID"]


def test_bare_lf_inside_a_cr_terminated_message_is_data_not_a_segment():
    # A reporting system left a raw line break in report text. With CR as the terminator, LF is data.
    m = parse(MSH + "\rOBX|1|TX|&GDT||line one\nline two||||||F\r")
    assert [s.name for s in m.segments] == ["MSH", "OBX"]
    assert "line one\nline two" in m.seg("OBX").raw(5)


def test_custom_delimiters():
    m = parse("MSH#$*!@#RIS#HOSP#V#B#20260101##ADT$A04#C9#P#2.5\rPID#1##X1$$$AUTH$MR*X2$$$A2$PI##FAM$GIV\r")
    assert m.delimiters.field == "#" and m.delimiters.component == "$" and m.delimiters.repetition == "*"
    assert m.delimiters.escape == "!" and m.delimiters.subcomponent == "@"
    assert m.type_label == "ADT^A04"
    assert [r.get(1) for r in m.seg("PID").reps(3)] == ["X1", "X2"]


def test_msh2_with_missing_trailing_encoding_chars():
    m = parse("MSH|^~|RIS|H|V|B|2026||ADT^A04|C|P|2.3\rPID|1||A&B^^^H\r")
    assert m.delimiters.escape is None and m.delimiters.subcomponent is None
    assert m.seg("PID").get(3) == "A&B"          # no subcomponent separator declared: & is data


def test_z_segments_are_kept():
    m = parse(MSH + "\rPID|1||1^^^H^MR\rZDS|1.2.3^RIS^Application^DICOM\rZPI|custom|data\r")
    assert [s.name for s in m.z_segments] == ["ZDS", "ZPI"]
    assert str(m.z_segments[1]) == "ZPI|custom|data"


@pytest.mark.parametrize("text, where", [
    ("", None),
    ("PID|1||x\r", "MSH"),
    ("MSH\r", "MSH"),
    ("MSH|^~\\&|A|B|C|D|2026||ADT^A04||P|2.5\r", "MSH-10 (message control id)"),
    ("MSH|^~\\&|A|B|C|D|2026|||C1|P|2.5\r", "MSH-9"),
    ("MSH|^~\\&|A|B|C|D|2026||ADT^A04|C1|P|\r", "MSH-12"),
    ("MSH|^~\\&|A|B|C|D|2026||ADT|C1|P|2.5\r", "MSH-9.2"),
    (MSH + "\rthis is not a segment\r", "invalid segment id"),
    (MSH + "\rPIDX|1\r", "invalid segment id"),
    (MSH + "\r" + MSH + "\r", "second MSH"),
    ("MSHa^~\\&|x\r", "field separator"),
    ("MSH|^^\\&|x\r", "encoding characters"),
])
def test_malformed_messages_raise_parse_error(text, where):
    with pytest.raises(ParseError) as e:
        parse(text)
    assert e.value.ack_code == "AR"
    if where:
        assert where in str(e.value)


# ---- character sets (MSH-18) -------------------------------------------------------------------
NAME = "NÚÑEZ^JOSÉ"


def _msg(charset: str) -> str:
    return f"MSH|^~\\&|RIS|H|V|B|2026||ADT^A04|C1|P|2.5.1||||||{charset}\rPID|1||1^^^H^MR||{NAME}\r"


@pytest.mark.parametrize("declared, codec", [("UNICODE UTF-8", "utf-8"), ("8859/1", "latin-1"), ("UTF-8", "utf-8"), ("8859/15", "iso8859-15")])
def test_declared_charset_is_honoured(declared, codec):
    raw = _msg(declared).encode(codec)
    m = parse_bytes(raw)
    assert m.seg("PID").get(5) == "NÚÑEZ" and m.seg("PID").get(5, 2) == "JOSÉ" and m.charset == codec
    assert declared_charset(raw) == declared


def test_undeclared_charset_utf8_then_cp1252_fallback():
    assert parse_bytes(_msg("").encode("utf-8")).seg("PID").get(5) == "NÚÑEZ"
    m = parse_bytes(_msg("").encode("cp1252"))
    assert m.seg("PID").get(5) == "NÚÑEZ" and m.charset == "cp1252"
    assert any("decoded as cp1252" in w for w in m.warnings)


def test_declared_charset_that_lies_is_refused_not_guessed():
    # MSH-18 says ASCII, the bytes are Latin-1: storing a mangled name on a patient is worse than an AE.
    with pytest.raises(CharsetError) as e:
        parse_bytes(_msg("ASCII").encode("latin-1"))
    assert e.value.ack_code == "AE" and "MSH-18" in str(e.value)
    with pytest.raises(CharsetError):
        parse_bytes(_msg("UNICODE UTF-8").encode("latin-1"))


def test_unknown_charset_name_falls_back_with_warning():
    text, codec, warnings = decode(_msg("KLINGON").encode("utf-8"))
    assert codec == "utf-8" and "KLINGON" in warnings[0]


def test_utf8_bom_is_ignored():
    m = parse_bytes(b"\xef\xbb\xbf" + _msg("UNICODE UTF-8").encode("utf-8"))
    assert m.msh.get(3) == "RIS"


def test_batch_split_keeps_each_message_bytes_intact():
    raw = b"FHS|^~\\&\r" + _msg("8859/1").encode("latin-1") + _msg("UNICODE UTF-8").encode("utf-8") + b"FTS|2\r"
    parts = split_batch_bytes(raw)
    assert len(parts) == 2
    assert parse_bytes(parts[0]).seg("PID").get(5) == "NÚÑEZ" and parse_bytes(parts[1]).seg("PID").get(5) == "NÚÑEZ"
    assert len(split_batch(MSH + "\n" + MSH + "\n")) == 2


def test_utf8_bytes_under_a_single_byte_declaration_are_refused():
    """8859/1 accepts every byte, so UTF-8 sent under that label would be stored as NÃ\x9aÃ\x91EZ with AA."""
    msg = ("MSH|^~\\&|RIS|H|V2FHIR|BRIDGE|20260101120000-0500||ADT^A04^ADT_A01|C1|P|2.5.1||||||8859/1\r"
           "EVN|A04\rPID|1||123^^^SYNTH_HOSP^MR||NÚÑEZ^JOSÉ\r")
    with pytest.raises(CharsetError):
        parse_bytes(msg.encode("utf-8"))
    with pytest.raises(CharsetError):
        parse_bytes(b"\xef\xbb\xbf" + msg.encode("latin-1"))
    assert parse_bytes(msg.encode("latin-1")).seg("PID").get(5) == "NÚÑEZ"      # real Latin-1 still decodes


def test_a_real_cp1252_message_that_happens_to_be_valid_utf8_is_accepted():
    """"RENÉ’S" in cp1252 is C9 92, which is also valid UTF-8 (U+0252). A correctly labelled cp1252 message must not be
    refused as mislabelled UTF-8 just because its bytes can be read that way."""
    msg = ("MSH|^~\\&|RIS|H|V2FHIR|BRIDGE|20260101120000-0500||ADT^A04^ADT_A01|C1|P|2.5.1||||||CP1252\r"
           "EVN|A04\rPID|1||123^^^SYNTH_HOSP^MR||RENÉ’S^JO\r")
    raw = msg.encode("cp1252")
    assert raw.count(b"\xc9\x92") == 1
    assert parse_bytes(raw).seg("PID").get(5) == "RENÉ’S"
