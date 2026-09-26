"""The mock FHIR server's transaction semantics (the idempotency tests are only as good as it is),
the CLI, and the one-command demo."""
import json

import httpx
import pytest

import run_demo
from mock_fhir.server import FhirStore, TxError
from tests.conftest import SAMPLES
from v2fhir.__main__ import main as cli
from v2fhir.mapping.bundle import token


def tx(*entries):
    return {"resourceType": "Bundle", "type": "transaction", "entry": list(entries)}


def pat(value, family="A", full="urn:uuid:p1"):
    return {"fullUrl": full, "resource": {"resourceType": "Patient", "identifier": [{"system": "s", "value": value}], "name": [{"family": family}]}}


def test_conditional_create_update_and_reference_rewrite():
    s = FhirStore()
    e = pat("1")
    e["request"] = {"method": "POST", "url": "Patient", "ifNoneExist": "identifier=s|1"}
    enc = {"fullUrl": "urn:uuid:e1", "resource": {"resourceType": "Encounter", "identifier": [{"system": "v", "value": "9"}], "status": "planned",
                                                  "class": {"code": "AMB"}, "subject": {"reference": "urn:uuid:p1"}},
           "request": {"method": "PUT", "url": "Encounter?identifier=v|9"}}
    r1 = s.transaction(tx(e, enc))
    assert [x["response"]["status"] for x in r1["entry"]] == ["201 Created", "201 Created"]
    pid = s.all("Patient")[0]["id"]
    assert s.all("Encounter")[0]["subject"]["reference"] == f"Patient/{pid}"
    r2 = s.transaction(tx(e, enc))
    assert [x["response"]["outcome"]["issue"][0]["details"]["coding"][0]["code"] for x in r2["entry"]] == ["matched", "unchanged"]
    assert s.all("Encounter")[0]["subject"]["reference"] == f"Patient/{pid}"    # reference resolved to the MATCHED patient
    assert s.counts() == {"Encounter": 1, "Patient": 1}


def test_transaction_is_all_or_nothing_and_ambiguous_match_is_412():
    s = FhirStore()
    for v in ("1", "2"):
        e = pat("dup", full=f"urn:uuid:{v}")
        e["request"] = {"method": "POST", "url": "Patient"}
        s.transaction(tx(e))
    good = pat("new", full="urn:uuid:n")
    good["request"] = {"method": "POST", "url": "Patient", "ifNoneExist": "identifier=s|new"}
    bad = pat("dup", full="urn:uuid:b")
    bad["request"] = {"method": "PUT", "url": "Patient?identifier=s|dup"}
    with pytest.raises(TxError) as e:
        s.transaction(tx(good, bad))
    assert e.value.status == 412 and s.counts() == {"Patient": 2}             # 'good' was rolled back


def test_conditional_patch_and_404():
    s = FhirStore()
    patch = {"fullUrl": "urn:uuid:x", "resource": {"resourceType": "Parameters", "parameter": [{"name": "operation", "part": [
        {"name": "type", "valueCode": "replace"}, {"name": "path", "valueString": "ServiceRequest.status"}, {"name": "value", "valueCode": "revoked"}]}]},
             "request": {"method": "PATCH", "url": "ServiceRequest?identifier=o|1"}}
    with pytest.raises(TxError) as e:
        s.transaction(tx(patch))
    assert e.value.status == 404
    s.transaction(tx({"resource": {"resourceType": "ServiceRequest", "identifier": [{"system": "o", "value": "1"}], "status": "active",
                                   "intent": "order", "subject": {"display": "x"}}, "request": {"method": "POST", "url": "ServiceRequest"}}))
    s.transaction(tx(patch))
    assert s.all("ServiceRequest")[0]["status"] == "revoked" and s.all("ServiceRequest")[0]["meta"]["versionId"] == "2"


def test_search_token_escaping_round_trip():
    s = FhirStore()
    e = {"resource": {"resourceType": "Patient", "identifier": [{"system": "http://x.org/a,b", "value": "12|34$5\\6"}]},
         "request": {"method": "POST", "url": "Patient"}}
    s.transaction(tx(e))
    from urllib.parse import parse_qsl
    q = parse_qsl("identifier=" + token("http://x.org/a,b", "12|34$5\\6"))
    assert len(s.search("Patient", q)) == 1
    assert s.search("Patient", parse_qsl("identifier=" + token("http://x.org/a,b", "12"))) == []


def test_http_endpoints(mock_server):
    with httpx.Client() as c:
        assert c.get(mock_server.base_url + "/metadata").json()["fhirVersion"] == "4.0.1"
        bad = c.post(mock_server.base_url + "/", content=b"{not json")
        assert bad.status_code == 400 and bad.json()["resourceType"] == "OperationOutcome"
        assert c.get(mock_server.base_url + "/Patient/999").status_code == 404
        assert c.get(mock_server.base_url + "/Patient?name=x").status_code == 400


# ---- CLI and demo ------------------------------------------------------------------------------------
def test_cli_convert_prints_a_valid_bundle(capsys):
    assert cli(["convert", str(SAMPLES / "oru_r01_final.hl7"), "--now", "2026-09-25T12:00:00+00:00"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["resourceType"] == "Bundle" and out["type"] == "transaction"
    assert {e["resource"]["resourceType"] for e in out["entry"]} >= {"DiagnosticReport", "ServiceRequest", "ImagingStudy", "Provenance"}


def test_cli_convert_reports_errors(tmp_path, capsys):
    f = tmp_path / "bad.hl7"
    f.write_bytes(b"MSH|^~\\&|A|B|C|D|2026||ADT^A04|1|P|2.5\rPID|1||\r")
    assert cli(["convert", str(f)]) == 1
    assert "PID-3" in capsys.readouterr().err


def test_demo_end_to_end(tmp_path, capsys):
    rows = run_demo.main(["--workdir", str(tmp_path)])
    assert [r["ack"] for r in rows] == ["AA"] * 10 + ["AR"]
    assert rows[9]["duplicate"] and rows[9]["ack_control_id"] == "RIS00002"
    out = capsys.readouterr().out
    assert "preliminary -> final -> corrected" in out and "NÚÑEZ-GARCÍA" in out
    assert len(list((tmp_path / "bundles").glob("*.json"))) == 9


def test_demo_works_when_a_system_proxy_is_configured(tmp_path, monkeypatch):
    """httpx picks up the system proxy (on Windows from the registry) but not the Windows <local> bypass, so the
    demo's 127.0.0.1 traffic went to the proxy and every message got an error ACK."""
    for var in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    rows = run_demo.main(["--workdir", str(tmp_path)])
    assert [r["ack"] for r in rows] == ["AA"] * 10 + ["AR"]
