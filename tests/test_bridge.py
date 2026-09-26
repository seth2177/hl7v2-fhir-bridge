"""End to end through the bridge into the mock FHIR server: idempotency, ordering traps, dedupe, NAKs."""
import json

import httpx

from tests.conftest import FIXED_NOW
from tools import ris_sim as r
from v2fhir.bridge import Bridge
from v2fhir.sink import FhirSink

O1 = dict(placer="ORD1001", filler="FIL5001", accession="ACC2001", procedure=r.CT_CHEST, modality="CT")
O2 = dict(placer="ORD1002", filler="FIL5002", accession="ACC2002", procedure=r.MR_BRAIN, modality="NMR")


def send(bridge, text: str | bytes):
    return bridge.handle(text.encode("utf-8") if isinstance(text, str) else text)


def store(bridge):
    return bridge.server.store


def test_same_orm_twice_gives_one_service_request(bridge):
    first = send(bridge, r.orm("NW", "C1", **O1))
    second = send(bridge, r.orm("NW", "C2", **O1))         # a genuine resend with a new control id
    assert first.ack_code == second.ack_code == "AA"
    counts = store(bridge).counts()
    assert counts["ServiceRequest"] == 1 and counts["Patient"] == 1 and counts["Practitioner"] == 1 and counts["Encounter"] == 1
    assert {e.resource_type: e.outcome for e in second.entries}["ServiceRequest"] == "matched"
    assert counts["Provenance"] == 2                        # two messages really were received


def test_replay_after_restart_is_idempotent_including_provenance(bridge, cfg, mock_server):
    send(bridge, r.orm("NW", "C1", **O1))
    fresh = Bridge(cfg)                                      # new process: dedupe cache is empty
    try:
        again = fresh.handle(r.orm("NW", "C1", **O1).encode())
    finally:
        fresh.close()
    assert again.ack_code == "AA" and not again.duplicate
    assert store(bridge).counts() == {"Encounter": 1, "Patient": 1, "Practitioner": 1, "Provenance": 1, "ServiceRequest": 1}


def test_duplicate_control_id_is_deduped_but_changed_content_is_not_dropped(bridge):
    msg = r.adt("A04", "DUP1")
    assert send(bridge, msg).ack_code == "AA"
    again = send(bridge, msg.replace("20260915080000", "20260915080500", 1))    # resend with a fresh MSH-7
    assert again.duplicate and again.ack_code == "AA" and not again.entries
    changed = send(bridge, r.adt("A04", "DUP1", name="DIFFERENT^PERSON"))
    assert not changed.duplicate and any("reused" in str(i) for i in changed.issues)
    assert store(bridge).all("Patient")[0]["name"][0]["family"] == "DIFFERENT"


def test_a08_updates_patient_version(bridge):
    send(bridge, r.adt("A04", "A1"))
    res = send(bridge, r.adt("A08", "A2", name="NÚÑEZ-GARCÍA^JOSÉ^ANTONIO^^^^L"))
    assert {e.resource_type: e.outcome for e in res.entries}["Patient"] == "updated"
    p = store(bridge).all("Patient")
    assert len(p) == 1 and p[0]["meta"]["versionId"] == "2" and p[0]["name"][0]["family"] == "NÚÑEZ-GARCÍA"
    same = send(bridge, r.adt("A08", "A3", name="NÚÑEZ-GARCÍA^JOSÉ^ANTONIO^^^^L"))
    assert {e.resource_type: e.outcome for e in same.entries}["Patient"] == "unchanged"


def test_order_lifecycle_and_report_versions(bridge):
    for m in (r.orm("NW", "O1", **O1), r.orm("SC", "O2", **O1, order_status="CM"),
              r.oru("P", "R1", **O1, findings=["a"], impression="prelim", study_uid=r.STUDY_UID_1),
              r.oru("F", "R2", **O1, findings=["a"], impression="final", study_uid=r.STUDY_UID_1),
              r.oru("C", "R3", **O1, findings=["a"], impression="corrected", study_uid=r.STUDY_UID_1)):
        assert send(bridge, m).ack_code == "AA"
    s = store(bridge)
    [sr] = s.all("ServiceRequest")
    [dr] = s.all("DiagnosticReport")
    assert sr["status"] == "completed"
    assert [v["status"] for v in s.history("DiagnosticReport", dr["id"])] == ["preliminary", "final", "corrected"]
    assert dr["basedOn"] == [{"reference": f"ServiceRequest/{sr['id']}"}]
    [study] = s.all("ImagingStudy")
    assert dr["imagingStudy"] == [{"reference": f"ImagingStudy/{study['id']}"}] and study["basedOn"] == dr["basedOn"]
    assert dr["subject"]["reference"] == sr["subject"]["reference"] == f"Patient/{s.all('Patient')[0]['id']}"


def test_oru_before_its_orm(bridge):
    res = send(bridge, r.oru("F", "R1", **O1, findings=["a"], impression="b"))
    assert res.ack_code == "AA" and {e.resource_type: e.outcome for e in res.entries}["ServiceRequest"] == "created"
    late = send(bridge, r.orm("NW", "O1", **O1))
    assert late.ack_code == "AA" and {e.resource_type: e.outcome for e in late.entries}["ServiceRequest"] == "matched"
    [sr] = store(bridge).all("ServiceRequest")
    assert sr["status"] == "completed"                     # the late NW did not re-open a reported order
    [dr] = store(bridge).all("DiagnosticReport")
    assert dr["basedOn"] == [{"reference": f"ServiceRequest/{sr['id']}"}]


def test_cancel_orc_only_patches_status(bridge):
    send(bridge, r.orm("NW", "O1", **O2))
    res = send(bridge, r.orm("CA", "O2", **O2, orc_only=True))
    assert res.ack_code == "AA" and {e.resource_type: e.outcome for e in res.entries}["ServiceRequest"] == "patched"
    [sr] = store(bridge).all("ServiceRequest")
    assert sr["status"] == "revoked" and sr["code"]["text"] == "MRI BRAIN W/O CONTRAST"      # nothing else wiped


def test_cancel_for_unknown_order(bridge):
    orc_only = send(bridge, r.orm("CA", "X1", **O2, orc_only=True))
    assert orc_only.ack_code == "AE" and orc_only.issues[0].code == "204"
    assert "ServiceRequest" not in store(bridge).counts()
    full = send(bridge, r.orm("CA", "X2", **O2))                                   # full order: recorded as revoked
    assert full.ack_code == "AA" and any(i.code == "204" and i.severity == "W" for i in full.issues)
    assert "ERR||ORC^1^2|204^Unknown key identifier^HL70357|W" in full.ack
    assert store(bridge).all("ServiceRequest")[0]["status"] == "revoked"


def test_malformed_message_gets_ar_and_nothing_is_written(bridge, tmp_path):
    res = send(bridge, b"MSH|^~\\&|RIS|H|V|B|2026||ORM^O01|BAD1|P|2.5\rPID|1\rnot a segment\r")
    assert res.ack_code == "AR" and "MSA|AR|BAD1|" in res.ack
    assert store(bridge).counts() == {} and not list((tmp_path / "bundles").glob("*.json"))


def test_unsupported_version_and_types(bridge, cfg):
    assert send(bridge, r.adt("A04", "V1").replace("|2.5.1|", "|2.6|")).ack_code == "AR"
    a03 = send(bridge, r.adt("A03", "D1"))
    assert a03.ack_code == "AA" and "acknowledged and ignored" in a03.issues[0].text and store(bridge).counts() == {}
    cfg.unsupported_messages = "reject"
    assert send(bridge, r.adt("A03", "D2")).ack_code == "AR"


def test_an_ack_is_never_acked(bridge):
    res = send(bridge, "MSH|^~\\&|X|Y|V|B|2026||ACK^A04^ACK|K1|P|2.5.1\rMSA|AA|C1\r")
    assert res.ack is None and res.ack_bytes is None


def test_mapping_error_is_ae_and_retry_after_fix_works(bridge):
    bad = send(bridge, r.adt("A04", "E1").replace(r.PATIENT["ids"], ""))
    assert bad.ack_code == "AE" and "ERR||PID^1^3|101^Required field missing^HL70357|E" in bad.ack
    assert send(bridge, r.adt("A04", "E1")).ack_code == "AA"            # AE was not cached as "seen"


def test_fhir_server_down_is_ae_and_resend_succeeds(cfg, tmp_path, mock_server):
    cfg.fhir_retries = 0
    down = Bridge(cfg, fhir_sink=FhirSink("http://127.0.0.1:9/fhir", timeout=2, retries=0))
    res = down.handle(r.adt("A04", "S1").encode())
    down.close()
    assert res.ack_code == "AE" and "unreachable" in res.issues[0].text and "safe to resend" in res.issues[0].text
    up = Bridge(cfg, fhir_sink=FhirSink(mock_server.base_url, retries=0))
    assert up.handle(r.adt("A04", "S1").encode()).ack_code == "AA"
    up.close()


def test_fhir_5xx_is_retried_4xx_is_not():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={"resourceType": "OperationOutcome", "issue": [{"diagnostics": "busy"}]})
        return httpx.Response(200, json={"resourceType": "Bundle", "type": "transaction-response",
                                         "entry": [{"response": {"status": "201 Created", "location": "Patient/1/_history/1"}}]})
    sink = FhirSink("http://fhir.invalid", retries=2, transport=httpx.MockTransport(handler))
    entries = sink.post({"resourceType": "Bundle", "type": "transaction", "entry": [{"request": {"method": "POST", "url": "Patient"}}]})
    assert len(calls) == 2 and entries[0].outcome == "created"
    calls.clear()
    sink = FhirSink("http://fhir.invalid", retries=2, transport=httpx.MockTransport(lambda req: (calls.append(1), httpx.Response(422, text="no"))[1]))
    try:
        sink.post({"resourceType": "Bundle", "type": "transaction", "entry": []})
    except Exception as e:  # noqa: BLE001
        assert "422" in str(e)
    assert len(calls) == 1


def test_bundle_files_use_safe_names(bridge, tmp_path):
    res = send(bridge, r.adt("A04", "../../etc/passwd"))
    assert res.ack_code == "AA"
    assert res.bundle_path.parent == tmp_path / "bundles" and ".." not in res.bundle_path.name
    assert json.loads(res.bundle_path.read_text(encoding="utf-8"))["identifier"]["value"] == "../../etc/passwd"


def test_non_ascii_names_end_to_end_in_both_encodings(bridge):
    latin = r.adt("A04", "L1").replace("UNICODE UTF-8", "8859/1").encode("latin-1")
    assert bridge.handle(latin).ack_code == "AA"
    assert store(bridge).all("Patient")[0]["name"][0]["given"][0] == "JOSÉ"
    res = bridge.handle(r.adt("A04", "L2").replace("UNICODE UTF-8", "ASCII").encode("latin-1"))
    assert res.ack_code == "AE" and "MSH-18" in res.issues[0].text


def test_late_preliminary_does_not_unsign_a_final_report(bridge):
    for m in (r.oru("F", "R2", **O1, findings=["a"], impression="final"),
              r.oru("P", "R1", **O1, findings=["a"], impression="prelim")):          # the queue retried an old message
        res = send(bridge, m)
    assert res.ack_code == "AA" and "late preliminary report ignored" in res.issues[0].text
    assert "DiagnosticReport" not in [e.resource_type for e in res.entries]
    [dr] = store(bridge).all("DiagnosticReport")
    assert dr["status"] == "final" and dr["meta"]["versionId"] == "1" and dr["conclusion"] == "final"
    corrected = send(bridge, r.oru("C", "R3", **O1, findings=["a"], impression="corrected"))
    assert {e.resource_type: e.outcome for e in corrected.entries}["DiagnosticReport"] == "updated"


def test_status_change_for_unknown_order_is_created_with_a_warning(bridge):
    res = send(bridge, r.orm("SC", "S1", **O1, order_status="CM"))
    assert res.ack_code == "AA" and "for unknown order ORD1001" in res.issues[0].text
    assert store(bridge).all("ServiceRequest")[0]["status"] == "completed"


def test_two_obr_groups_for_one_order_in_one_oru(cfg):
    from v2fhir.convert import convert
    from v2fhir.hl7.parser import parse
    msg = r.oru("F", "R1", **O1, findings=["a"], impression="b")
    obr = next(s for s in msg.split("\r") if s.startswith("OBR"))
    conv = convert(parse(msg + obr.replace("OBR|1|", "OBR|2|") + "\rOBX|1|TX|&GDT||addendum||||||F\r"), cfg)
    assert len([e for e in conv.bundle["entry"] if e["resource"]["resourceType"] == "DiagnosticReport"]) == 1
    assert any("second OBR" in str(w) for w in conv.warnings)


# ---- directory sink: one file per distinct message ------------------------------------------------------
def _families_on_disk(root) -> set[str]:
    out = set()
    for p in root.glob("*.json"):
        b = json.loads(p.read_text(encoding="utf-8"))
        out |= {e["resource"]["name"][0]["family"] for e in b["entry"] if e["resource"]["resourceType"] == "Patient"}
    return out


def test_reused_control_id_or_second_facility_does_not_overwrite_an_accepted_bundle(cfg, tmp_path):
    cfg.out_dir = str(tmp_path / "bundles")
    bridge = Bridge(cfg, clock=lambda: FIXED_NOW)
    first = bridge.handle(r.adt("A04", "C1").encode())
    reused = bridge.handle(r.adt("A04", "C1", name="OTHER^PATIENT").encode())          # counter reset: new message, old MSH-10
    other_site = bridge.handle(r.adt("A04", "C1", name="THIRD^PATIENT").replace("|RIS_SIM|SYNTH_HOSP|", "|RIS_SIM|OTHER_HOSP|", 1).encode())
    resend = bridge.handle(r.adt("A04", "C1").encode())
    assert {first.ack_code, reused.ack_code, other_site.ack_code, resend.ack_code} == {"AA"}
    assert len({first.bundle_path, reused.bundle_path, other_site.bundle_path}) == 3
    assert resend.bundle_path == first.bundle_path                     # the same content lands on its own file again
    assert _families_on_disk(tmp_path / "bundles") == {"NÚÑEZ", "OTHER", "THIRD"}
