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


# ---- an A40 merge must survive routine ADT traffic ---------------------------------------------------------
def _patient(store, mrn):
    found = [p for p in store.all("Patient") if any(i.get("value") == mrn for i in p.get("identifier", []))]
    assert len(found) == 1, f"expected one Patient with MRN {mrn}, got {len(found)}"
    return found[0]


@pytest.fixture
def merged(bridge):
    samples = ROOT / "samples"
    assert bridge.handle((samples / "adt_a04_register.hl7").read_bytes()).ack_code == "AA"
    assert bridge.handle((samples / "adt_a40_merge.hl7").read_bytes()).ack_code == "AA"     # SYN100999 -> SYN100234
    return bridge, bridge.server.store


def test_survivor_keeps_its_merge_link_after_a_routine_a08(merged):
    """PID carries no links, so a plain PUT from the next A08 erased the survivor's 'replaces' link."""
    bridge, store = merged
    retired_id = _patient(store, "SYN100999")["id"]
    assert bridge.handle((ROOT / "samples" / "adt_a08_name_change.hl7").read_bytes()).ack_code == "AA"
    assert {"other": {"reference": f"Patient/{retired_id}"}, "type": "replaces"} in _patient(store, "SYN100234").get("link", [])


def test_late_adt_for_the_merged_away_mrn_does_not_reactivate_it(merged):
    """R4 Patient.active: updates for an inactive record linked to an active one belong on the other record."""
    bridge, store = merged
    survivor_id = _patient(store, "SYN100234")["id"]
    late = (ROOT / "samples" / "adt_a08_name_change.hl7").read_bytes().replace(b"SYN100234", b"SYN100999").replace(b"RIS00004", b"OTH00001")
    res = bridge.handle(late)
    retired = _patient(store, "SYN100999")
    assert retired["active"] is False and {"other": {"reference": f"Patient/{survivor_id}"}, "type": "replaced-by"} in retired["link"]
    assert res.ack_code == "AA" and any("merged into" in str(i) for i in res.issues)


# ---- provider ids without an assigning authority ------------------------------------------------------------
def _seg(name: str, fields: dict[int, str]) -> str:
    return name + "|" + "|".join(fields.get(i, "") for i in range(1, max(fields) + 1)) + "\r"


def _head(kind: str, control: str, app: str) -> str:
    return (f"MSH|^~\\&|{app}|SYNTH_HOSP|V2FHIR|BRIDGE|20260915120000-0500||{kind}|{control}|P|2.5.1\r"
            "PID|1||SYN1^^^SYNTH_HOSP^MR||DOE^JANE||19700101|F\r")


def _orm() -> str:   # from the RIS: ordering provider 1234 in the RIS's numbering
    return (_head("ORM^O01^ORM_O01", "O1", "RIS")
            + _seg("ORC", {1: "NW", 2: "ORD1^SYNTH_HIS", 3: "FIL1^SYNTH_RIS", 5: "SC", 12: "1234^SMITH^JOHN"})
            + _seg("OBR", {1: "1", 2: "ORD1^SYNTH_HIS", 3: "FIL1^SYNTH_RIS", 4: "71250^CT CHEST^C4", 18: "ACC1", 24: "CT"}))


def _oru(orc12: str | None) -> str:   # from the reporting system: radiologist 1234 in ITS numbering
    orc = {1: "RE", 2: "ORD1^SYNTH_HIS", 3: "FIL1^SYNTH_RIS", 5: "CM"}
    if orc12:
        orc[12] = orc12          # the ordering provider echoed from the RIS order
    return (_head("ORU^R01^ORU_R01", "R1", "REPORTING")
            + _seg("ORC", orc)
            + _seg("OBR", {1: "1", 2: "ORD1^SYNTH_HIS", 3: "FIL1^SYNTH_RIS", 4: "71250^CT CHEST^C4", 7: "20260915101200-0500",
                          18: "ACC1", 22: "20260915120000-0500", 24: "CT", 25: "F", 32: "1234&JONES&MARY"})
            + "OBX|1|TX|&IMP^Impression|1|IMPRESSION: normal.||||||F\r")


def _cfg(**over):
    return load_config(ROOT / "config" / "bridge.toml", {"out_dir": "", "fhir_base_url": "", **over})


def test_interpreter_reference_never_resolves_to_a_differently_named_provider_in_one_bundle():
    conv = convert(parse(_oru("1234^SMITH^JOHN")), _cfg(), NOW)
    entries = {e["fullUrl"]: e["resource"] for e in conv.bundle["entry"]}
    dr = next(r for r in entries.values() if r["resourceType"] == "DiagnosticReport")
    interp = dr["resultsInterpreter"][0]
    warned = any("1234" in w.text for w in conv.warnings)
    if "reference" in interp:
        target = entries[interp["reference"]]
        assert target["name"][0]["family"] == "JONES", (
            f"resultsInterpreter display {interp.get('display')!r} but its reference resolves to {target['name'][0]} "
            f"(warned={warned})")
    else:
        assert warned, "display-only interpreter without telling anyone why"


def test_unqualified_provider_id_across_messages_is_at_least_reported():
    srv = MockFhirServer().start()
    try:
        bridge = Bridge(_cfg(fhir_base_url=srv.base_url, fhir_retries=0), clock=lambda: NOW)
        assert bridge.handle(_orm().encode()).ack_code == "AA"
        res = bridge.handle(_oru(None).encode())
        bridge.close()
        dr = srv.store.all("DiagnosticReport")[0]
        interp = dr["resultsInterpreter"][0]
        warned = any("1234" in i.text for i in res.issues)
        if "reference" in interp:
            who = srv.store.current("Practitioner", interp["reference"].split("/")[-1])
            assert who["name"][0]["family"] == "JONES" or warned, (
                f"signed report attributed to Practitioner {who['name'][0]} (ACK {res.ack_code}, no warning)")
        else:
            assert warned
    finally:
        srv.stop()
