"""Patient and Encounter match keys must always carry a system. FHIR R4 search.html#token: identifier=1001
matches 1001 in ANY identifier system, and a conditional update with one match replaces that resource, so a
bare-value key overwrites whichever patient happens to hold that number. HL7 table 0301 (HD-3) is open-ended
(DNS, L, GUID...), and senders also send a universal id without HD-3 or a mistyped OID."""
from urllib.parse import parse_qsl

import pytest

from mock_fhir import MockFhirServer
from mock_fhir.server import FhirStore
from tests.conftest import FIXED_NOW as NOW
from tests.conftest import ROOT
from v2fhir.bridge import Bridge
from v2fhir.config import load_config
from v2fhir.convert import convert
from v2fhir.errors import MappingError
from v2fhir.hl7.parser import parse
from v2fhir.mapping.bundle import token


def adt(control: str, pid3: str, family: str, pv1_19: str = "V1^^^SYNTH_HOSP^VN") -> str:
    return (f"MSH|^~\\&|ADT_SIM|SYNTH_HOSP|V2FHIR|BRIDGE|20260915080000-0500||ADT^A04^ADT_A01|{control}|P|2.5.1\r"
            "EVN|A04|20260915080000-0500\r"
            f"PID|1||{pid3}||{family}^PAT^^^^^L||19700101|F\r"
            f"PV1|1|O|||||||||||||||||{pv1_19}\r")


def cfg(**over):
    return load_config(ROOT / "config" / "bridge.toml", {"out_dir": "", "fhir_base_url": "", **over})


def match_tokens(entry: dict) -> list[str]:
    """The identifier tokens a conditional request matches on (PUT url query or POST ifNoneExist)."""
    req = entry["request"]
    query = req.get("ifNoneExist") or req["url"].partition("?")[2]
    [(name, value)] = parse_qsl(query, keep_blank_values=True)
    assert name == "identifier"
    return value.split(",")


def keys(msg: str, c) -> dict[str, list[str]] | None:
    """{resourceType: tokens} for Patient and Encounter, or None when the bridge refuses (AE)."""
    try:
        b = convert(parse(msg), c, NOW).bundle
    except MappingError:
        return None
    return {e["resource"]["resourceType"]: match_tokens(e) for e in b["entry"]
            if e["resource"]["resourceType"] in ("Patient", "Encounter")}


UNRESOLVABLE_HD = [
    "&hospa.example.org&DNS",          # table 0301 DNS
    "&LOCAL_MRN&L",                    # table 0301 L (local)
    "&2.16.840.1.113883.3.999",        # universal id without HD-3
    "&2.16.840.1.113883.19.05&ISO",    # ISO with a malformed arc (leading zero)
]


@pytest.mark.parametrize("hd", UNRESOLVABLE_HD)
def test_patient_and_encounter_are_never_matched_on_a_bare_value(hd):
    k = keys(adt("C1", f"1001^^^{hd}^MR", "ALPHA", f"V1^^^{hd}^VN"), cfg())
    if k is None:
        return                                           # AE is an acceptable, fail-closed answer
    for rtype, toks in k.items():
        for t in toks:
            assert "|" in t, f"{rtype} matched on {t!r}: an any-system token (R4 search.html#token)"


def test_two_different_authorities_do_not_share_one_key():
    """1001 at hospa.example.org and 1001 at clinic.example.org are different people."""
    a = keys(adt("C1", "1001^^^&hospa.example.org&DNS^MR", "ALPHA"), cfg())
    b = keys(adt("C2", "1001^^^&clinic.example.org&DNS^MR", "BRAVO"), cfg())
    if a is None or b is None:
        return
    assert a["Patient"] != b["Patient"], f"two assigning authorities collapse into one match key: {a['Patient']}"


def test_empty_cx4_without_a_configured_default_is_not_matched_on_a_bare_value():
    k = keys(adt("C1", "1001^^^^MR", "ALPHA", "V1"), cfg(default_assigning_authority=""))
    if k is None:
        return
    for rtype, toks in k.items():
        assert all("|" in t for t in toks), f"{rtype} matched on {toks}: any-system token"


def test_adt_from_another_authority_does_not_overwrite_a_different_patient():
    """ALPHA carries enterprise id 1001 (PI). BRAVO, a different person, arrives with MRN 1001 from a clinic
    whose authority is sent as a DNS universal id. BRAVO must not replace ALPHA (AE is acceptable)."""
    srv = MockFhirServer().start()
    try:
        bridge = Bridge(cfg(fhir_base_url=srv.base_url, fhir_retries=0), clock=lambda: NOW)
        assert bridge.handle(adt("C1", "SYN1^^^SYNTH_HOSP^MR~1001^^^SYNTH_EMPI^PI", "ALPHA").encode()).ack_code == "AA"
        r2 = bridge.handle(adt("C2", "1001^^^&clinic.example.org&DNS^MR", "BRAVO", "CV1^^^&clinic.example.org&DNS^VN").encode())
        bridge.close()
        alpha = [p for p in srv.store.all("Patient") if any(i.get("value") == "SYN1" for i in p.get("identifier", []))]
        assert alpha and alpha[0]["name"][0]["family"] == "ALPHA", \
            f"BRAVO's A04 (ACK {r2.ack_code}) overwrote ALPHA: {[p['name'][0]['family'] for p in srv.store.all('Patient')]}"
    finally:
        srv.stop()


def test_token_for_an_identifier_without_system_uses_the_no_system_form():
    """R4 search.html#token: the token for "value 12345, no system" is `|12345`; `12345` means any system."""
    assert token(None, "12345") == "|12345"


def test_mock_implements_the_no_system_token_form():
    """Mock fidelity: `identifier=|12345` matches only identifiers WITHOUT a system (R4 search.html#token)."""
    s = FhirStore()
    s.transaction({"resourceType": "Bundle", "type": "transaction", "entry": [
        {"resource": {"resourceType": "Patient", "identifier": [{"value": "12345"}]}, "request": {"method": "POST", "url": "Patient"}},
        {"resource": {"resourceType": "Patient", "identifier": [{"system": "http://other.example.org/mrn", "value": "12345"}]},
         "request": {"method": "POST", "url": "Patient"}}]})
    assert len(s.search("Patient", [("identifier", "|12345")])) == 1
    assert len(s.search("Patient", [("identifier", "12345")])) == 2
