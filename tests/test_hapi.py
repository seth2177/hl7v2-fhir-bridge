"""The demo traffic against a real FHIR server. Skipped unless HAPI_URL is set, e.g.

    docker run -p 8080:8080 hapiproject/hapi:latest
    HAPI_URL=http://127.0.0.1:8080/fhir python -m pytest tests/test_hapi.py

Use an empty server: the test checks exact resource counts."""
import os
import uuid

import httpx
import pytest

from v2fhir.bridge import Bridge
from v2fhir.tools import ris_sim as r

HAPI_URL = os.environ.get("HAPI_URL")
pytestmark = pytest.mark.skipif(not HAPI_URL, reason="set HAPI_URL to run against a real FHIR server")
TYPES = ("Patient", "Practitioner", "Encounter", "ServiceRequest", "DiagnosticReport", "ImagingStudy", "Provenance")


def _total(client: httpx.Client, rtype: str, **params) -> int:
    # _summary=count can be cached by HAPI; a fresh search with a unique no-op parameter is not
    return client.get(f"{HAPI_URL}/{rtype}", params={"_total": "accurate", "_count": 0, "_elements": "id", **params},
                      headers={"Cache-Control": "no-cache"}).json()["total"]


def test_demo_traffic_and_a_replay_after_restart_on_a_real_server(cfg):
    cfg.fhir_base_url, cfg.out_dir = HAPI_URL, ""
    client = httpx.Client(trust_env=False, timeout=30)
    first = Bridge(cfg)
    assert [first.handle(m.encode()).ack_code for _, m in r.demo_script()] == ["AA"] * 9
    first.close()
    counts = {t: _total(client, t) for t in TYPES}
    assert counts == {"Patient": 1, "Practitioner": 2, "Encounter": 1, "ServiceRequest": 2, "DiagnosticReport": 1,
                      "ImagingStudy": 1, "Provenance": 9}, counts
    again = Bridge(cfg)                                     # a restart: the dedupe cache is empty
    assert [again.handle(m.encode()).ack_code for _, m in r.demo_script()] == ["AA"] * 9
    again.close()
    assert {t: _total(client, t) for t in TYPES} == counts, "a replay created resources"
    [dr] = client.get(f"{HAPI_URL}/DiagnosticReport").json()["entry"]
    assert dr["resource"]["status"] == "corrected" and "LOWER" in dr["resource"]["conclusion"]


def test_search_special_characters_in_order_numbers_on_a_real_server(cfg):
    cfg.fhir_base_url, cfg.out_dir = HAPI_URL, ""
    tag = uuid.uuid4().hex[:6]
    nums = dict(placer=f"P,{tag}\\F\\X", filler=f"F${tag}", accession=f"A {tag}", procedure=r.CT_CHEST, modality="CT")
    b = Bridge(cfg)
    assert b.handle(r.orm("NW", f"S1{tag}", **nums).encode()).ack_code == "AA"
    assert b.handle(r.orm("NW", f"S2{tag}", **nums).encode()).ack_code == "AA"
    b.close()
    client = httpx.Client(trust_env=False, timeout=30)
    assert _total(client, "ServiceRequest", identifier=f"http://example.org/fhir/sid/synth-his|P\\,{tag}\\|X") == 1
