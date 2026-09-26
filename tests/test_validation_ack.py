"""The validator catches what fhir.resources alone would not, and ACKs are built correctly per version."""
import copy

import pytest

from tests.conftest import to_bundle
from tools import ris_sim as r
from v2fhir.bridge import Bridge
from v2fhir.errors import Issue, Location
from v2fhir.hl7.ack import build_ack, head_fields
from v2fhir.hl7.parser import parse, split_batch_bytes
from v2fhir.validate import BundleInvalid, validate_bundle


# ---- validation ------------------------------------------------------------------------------------
@pytest.fixture
def adt_bundle(cfg):
    return to_bundle(r.adt("A04", "C1"), cfg)


@pytest.mark.parametrize("mutate, expect", [
    (lambda b: b["entry"][0]["resource"].__setitem__("gender", "bogus"), "Patient.gender"),          # models accept this; bindings don't
    (lambda b: b["entry"][0]["resource"].__setitem__("birthDate", "1968-13-01"), "birthDate"),
    (lambda b: b["entry"][0]["resource"].__setitem__("colour", "blue"), "colour"),
    (lambda b: b["entry"][0]["resource"]["name"][0].__setitem__("use", "legal"), "name.use"),
    (lambda b: b["entry"][2]["resource"].__setitem__("status", "done"), "Encounter.status"),
    (lambda b: b["entry"][2]["resource"]["period"].__setitem__("start", "2026-09-15T08:00:00"), "start"),   # time without offset
    (lambda b: b["entry"][0]["request"].__setitem__("method", "FETCH"), "method"),
])
def test_validator_rejects(adt_bundle, mutate, expect):
    b = copy.deepcopy(adt_bundle)
    b["entry"][2]["resource"].setdefault("period", {"start": "2026-09-15T08:00:00-05:00"})
    validate_bundle(b)
    mutate(b)
    with pytest.raises(BundleInvalid) as e:
        validate_bundle(b)
    assert expect in str(e.value)


def test_validator_checks_patch_status_codes(cfg):
    b = to_bundle(r.orm("CA", "C", placer="ORD1", filler="F1", accession="A1", procedure=r.CT_CHEST, modality="CT", orc_only=True), cfg)
    b["entry"][0]["resource"]["parameter"][0]["part"][2]["valueCode"] = "cancelled"
    with pytest.raises(BundleInvalid, match="PATCH value"):
        validate_bundle(b)


# ---- ACK ---------------------------------------------------------------------------------------------
def segs(ack: str) -> dict[str, list[str]]:
    return {s[:3]: s.split("|") for s in ack.split("\r") if s}


def test_aa_swaps_sender_and_receiver_and_echoes_control_id():
    msg = parse(r.adt("A04", "RIS00001"))
    ack = build_ack("AA", msg=msg)
    s = segs(ack)
    assert s["MSH"][2:6] == ["V2FHIR", "BRIDGE", "RIS_SIM", "SYNTH_HOSP"]
    assert s["MSH"][8] == "ACK^A04^ACK" and s["MSH"][10] == "P" and s["MSH"][11] == "2.5.1" and s["MSH"][17] == "UNICODE UTF-8"
    assert s["MSA"] == ["MSA", "AA", "RIS00001"] and "ERR" not in s
    assert s["MSH"][9] != "RIS00001" and len(s["MSH"][9]) <= 20
    assert ack.endswith("\r")


def test_ae_with_err_v25_layout():
    msg = parse(r.adt("A04", "C9"))
    ack = build_ack("AE", msg=msg, issues=[Issue("101", "no patient identifier (PID-3 is empty)", Location("PID", 1, 3), "E")])
    s = segs(ack)
    assert s["MSA"][:3] == ["MSA", "AE", "C9"] and "PID-3" in s["MSA"][3]
    assert s["ERR"][2] == "PID^1^3" and s["ERR"][3] == "101^Required field missing^HL70357" and s["ERR"][4] == "E"
    assert s["ERR"][8] == "no patient identifier (PID-3 is empty)"


def test_err_v23_layout_and_no_warnings_on_v23():
    msg = parse(r.adt("A04", "C9").replace("|2.5.1|", "|2.3|"))
    ack = build_ack("AE", msg=msg, issues=[Issue("103", "bad | code", Location("ORC", 1, 1), "E"), Issue("0", "just a warning", None, "W")])
    s = segs(ack)
    assert s["MSH"][8] == "ACK^A04"                                  # no message structure component before 2.3.1
    assert s["ERR"][1] == "ORC^1^1^103&bad \\F\\ code&HL70357"        # free text escaped
    assert ack.count("ERR|") == 1                                    # the warning is not sent to a v2.3 receiver
    warn = build_ack("AA", msg=parse(r.adt("A04", "C9")), issues=[Issue("0", "just a warning", None, "W")])
    assert segs(warn)["ERR"][4] == "W"


def test_ack_uses_the_senders_delimiters():
    msg = parse("MSH#$*!@#RIS#HOSP#V#B#20260101##ADT$A04#C9#P#2.5\rPID#1##X1$$$AUTH$MR\r")
    ack = build_ack("AR", msg=msg, issues=[Issue("100", "a#b", None, "E")])
    assert ack.startswith("MSH#$*!@#V#B#RIS#HOSP#") and "MSA#AR#C9#a!F!b" in ack


def test_nak_for_unparseable_input_recovers_msh():
    raw = b"MSH|^~\\&|RIS|HOSP|V|B|2026||ORM^O01|CTRL42|P|2.4\rPID|bad\xff\x00\rgarbage line\r"
    head = head_fields(raw)
    assert head["10"] == "CTRL42" and head["12"] == "2.4" and head["9.2"] == "O01"
    ack = build_ack("AR", head=head, issues=[Issue("100", "invalid segment", None, "E")])
    s = segs(ack)
    assert s["MSA"][:3] == ["MSA", "AR", "CTRL42"] and s["MSH"][4:6] == ["RIS", "HOSP"] and s["MSH"][11] == "2.4"
    assert head_fields(b"\x00\x01 total garbage") == {}
    assert segs(build_ack("AR", head={}, issues=[]))["MSA"][:3] == ["MSA", "AR", ""]


# ---- a UTF-8 BOM in front of MSH, and NAKs in the sender's character set ------------------------------------
def test_ar_for_a_bom_prefixed_message_that_does_not_parse_still_echoes_msa2(cfg):
    raw = b"\xef\xbb\xbfMSH|^~\\&|RIS|HOSP|V2FHIR|BRIDGE|20260101||ADT^A04|CTRL77|P|2.5.1\rpid|1\r"
    result = Bridge(cfg).handle(raw)
    msh, msa = [s.split("|") for s in result.ack.split("\r")[:2]]
    assert result.ack_code == "AR" and msa[2] == "CTRL77" and msh[4:6] == ["RIS", "HOSP"]


@pytest.mark.parametrize("codec, declared", [("latin-1", "8859/1"), ("utf-8", "UNICODE UTF-8")])
def test_nak_echoes_a_non_ascii_sender_in_the_declared_charset(cfg, codec, declared):
    raw = f"MSH|^~\\&|RIS|HÔPITAL|V2FHIR|BRIDGE|20260101||ADT^A04|C9|P|2.5.1||||||{declared}\rpid|1\r".encode(codec)
    result = Bridge(cfg).handle(raw)
    msh = result.ack_bytes.decode(codec).split("\r")[0].split("|")
    assert result.ack_code == "AR" and msh[17] == declared and msh[5] == "HÔPITAL"


def test_bom_prefixed_batch_file_yields_only_the_messages():
    raw = (b"\xef\xbb\xbfFHS|^~\\&|RIS\r\nBHS|^~\\&|RIS\r\n"
           b"MSH|^~\\&|RIS|H|V|B|2026||ADT^A04|C1|P|2.5.1\r\nPID|1||1^^^H^MR\r\nBTS|1\r\nFTS|1\r\n")
    parts = split_batch_bytes(raw)
    assert len(parts) == 1 and parts[0].startswith(b"MSH|")
