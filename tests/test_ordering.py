"""An older message must never undo newer state on the FHIR server. Conditional requests cannot say "only if
newer", so the bridge reads orders and reports before writing them (bridge._reconcile). These tests replay the
realistic ways an old message arrives late: a resend after a restart (the dedupe cache is in memory), an AE'd
message reprocessed from the engine's error queue, and a queued message that was simply delivered late."""
import re
from datetime import timedelta

import pytest

from tests.conftest import FIXED_NOW, sample_bytes
from tools import ris_sim as r
from v2fhir.bridge import Bridge
from v2fhir.sink import FhirError, FhirSink

O1 = dict(placer="ORD1001", filler="FIL5001", accession="ACC2001", procedure=r.CT_CHEST, modality="CT")


@pytest.fixture
def server_cfg(cfg, mock_server, tmp_path):
    cfg.fhir_base_url = mock_server.base_url
    cfg.out_dir = str(tmp_path / "bundles")
    cfg.fhir_retries = 0
    return cfg


def _bridge(cfg, hours: int = 0) -> Bridge:
    """A fresh Bridge is a restart: its dedupe cache is empty."""
    return Bridge(cfg, clock=lambda: FIXED_NOW + timedelta(hours=hours))


class FlakySink(FhirSink):
    """The FHIR server answers 503 to the next POST only."""
    fail_next = False

    def post(self, bundle, deadline=None):
        if self.fail_next:
            self.fail_next = False
            raise FhirError("FHIR server error 503: busy", 503, True)
        return super().post(bundle, deadline)


# ---- reports: HL7 table 0123 F "can only be changed with a corrected result" ----------------------------
def test_final_resent_after_restart_does_not_revert_the_correction(server_cfg, mock_server):
    steps = dict(r.demo_script())
    first = _bridge(server_cfg)
    for name in ("register outpatient", "new CT order", "exam completed (SC/CM)", "preliminary report", "final report", "corrected report"):
        assert first.handle(steps[name].encode()).ack_code == "AA", name
    first.close()
    restarted = _bridge(server_cfg, 1)
    res = restarted.handle(steps["final report"].encode())          # the engine resends RPT00002 byte for byte
    restarted.close()
    [dr] = mock_server.store.all("DiagnosticReport")
    assert dr["status"] == "corrected" and "LOWER" in dr["conclusion"]
    assert res.ack_code == "AA" and any("late final report ignored" in str(i) for i in res.issues)


def test_final_reprocessed_from_the_error_queue_does_not_revert_the_correction(server_cfg, mock_server):
    """No restart needed: the final got AE, the correction went through, then someone reprocessed the final."""
    sink = FlakySink(mock_server.base_url, retries=0)
    bridge = Bridge(server_cfg, fhir_sink=sink, clock=lambda: FIXED_NOW)
    final = r.oru("F", "RPT2", **O1, findings=["nodule right UPPER lobe"], impression="IMPRESSION: right upper lobe", when="20260915120000")
    corrected = r.oru("C", "RPT3", **O1, findings=["nodule right LOWER lobe"], impression="IMPRESSION: CORRECTED: right LOWER lobe",
                      when="20260915130000")
    sink.fail_next = True
    assert bridge.handle(final.encode()).ack_code == "AR"
    assert bridge.handle(corrected.encode()).ack_code == "AA"
    bridge.handle(final.encode())
    bridge.close()
    [dr] = mock_server.store.all("DiagnosticReport")
    assert dr["status"] == "corrected" and "LOWER" in dr["conclusion"]


def test_late_cancelled_result_does_not_unsign_a_final_report(server_cfg, mock_server):
    bridge = _bridge(server_cfg)
    final = sample_bytes("oru_r01_final.hl7")
    assert bridge.handle(final).ack_code == "AA"
    segments = re.split(rb"\r\n|\r|\n", final.replace(b"|CT|F|", b"|CT|X|").replace(b"RPT00002", b"RPT00000"))
    bridge.handle(b"\r".join(s for s in segments if s and not s.startswith(b"OBX")))
    bridge.close()
    [dr] = mock_server.store.all("DiagnosticReport")
    assert dr["status"] == "final" and dr["conclusion"].startswith("IMPRESSION") and dr["presentedForm"]


def test_older_correction_resent_after_a_newer_one_is_ignored(server_cfg, mock_server):
    """Same status: OBR-22 (DiagnosticReport.issued) decides."""
    c1 = r.oru("C", "RPT3", **O1, findings=["a"], impression="IMPRESSION: first correction", when="20260915130000")
    c2 = r.oru("C", "RPT4", **O1, findings=["a"], impression="IMPRESSION: second correction", when="20260915140000")
    first = _bridge(server_cfg)
    for m in (r.oru("F", "RPT2", **O1, findings=["a"], impression="IMPRESSION: final", when="20260915120000"), c1, c2):
        assert first.handle(m.encode()).ack_code == "AA"
    first.close()
    restarted = _bridge(server_cfg, 1)
    restarted.handle(c1.encode())
    restarted.close()
    [dr] = mock_server.store.all("DiagnosticReport")
    assert dr["conclusion"] == "IMPRESSION: second correction"


# ---- orders: R4 request-status completed / revoked are terminal -------------------------------------------
def test_resent_full_status_change_does_not_revive_a_cancelled_order(server_cfg, mock_server):
    sc = r.orm("SC", "RIS3", **O1, order_status="CM")
    first = _bridge(server_cfg)
    for m in (r.orm("NW", "RIS2", **O1), sc, r.orm("CA", "RIS4", **O1, orc_only=True)):
        assert first.handle(m.encode()).ack_code == "AA"
    first.close()
    restarted = _bridge(server_cfg, 1)
    res = restarted.handle(sc.encode())
    restarted.close()
    [sr] = mock_server.store.all("ServiceRequest")
    assert sr["status"] == "revoked" and any("late order status completed ignored" in str(i) for i in res.issues)


def test_resent_orc_only_release_does_not_revive_a_cancelled_order(server_cfg, mock_server):
    rl = r.orm("RL", "RIS4", **O1, orc_only=True)
    first = _bridge(server_cfg)
    for m in (r.orm("NW", "RIS2", **O1), r.orm("HD", "RIS3", **O1, orc_only=True), rl, r.orm("CA", "RIS5", **O1, orc_only=True)):
        assert first.handle(m.encode()).ack_code == "AA"
    first.close()
    restarted = _bridge(server_cfg, 1)
    res = restarted.handle(rl.encode())                 # an ORC-only message: the PATCH and its Provenance both go
    restarted.close()
    [sr] = mock_server.store.all("ServiceRequest")
    assert sr["status"] == "revoked" and res.ack_code == "AA"


def test_a_completed_order_can_still_be_cancelled(server_cfg, mock_server):
    bridge = _bridge(server_cfg)
    for m in (r.orm("NW", "RIS2", **O1), r.orm("SC", "RIS3", **O1, order_status="CM"), r.orm("CA", "RIS4", **O1, orc_only=True)):
        assert bridge.handle(m.encode()).ack_code == "AA"
    bridge.close()
    assert mock_server.store.all("ServiceRequest")[0]["status"] == "revoked"


# ---- one shared number, two different orders: AE 205, never a silent merge ---------------------------------
CHEST = dict(placer="ORD1", filler="FIL1", accession="ACC1", procedure=r.CT_CHEST, modality="CT")
ABDOMEN = dict(placer="ORD2", filler="FIL2", accession="ACC1", procedure="74176^CT ABD PELVIS W/O CONTRAST^C4", modality="CT")


def test_report_for_another_order_sharing_the_accession_is_refused(server_cfg, mock_server):
    """The OR match (placer, filler or accession) hits ONE stored order, so the server sees no conflict; the
    placer and filler of the same system disagree, so only the client can tell these are two orders."""
    bridge = _bridge(server_cfg)
    assert bridge.handle(r.orm("NW", "O1", **CHEST).encode()).ack_code == "AA"
    assert bridge.handle(r.oru("F", "R1", **CHEST, findings=["chest"], impression="IMPRESSION: CHEST normal").encode()).ack_code == "AA"
    res = bridge.handle(r.oru("F", "R2", **ABDOMEN, findings=["abdomen"], impression="IMPRESSION: ABDOMEN normal").encode())
    bridge.close()
    assert res.ack_code == "AE" and res.issues[0].code == "205"
    assert [d["conclusion"] for d in mock_server.store.all("DiagnosticReport")] == ["IMPRESSION: CHEST normal"]


def test_new_order_sharing_only_the_accession_is_refused_not_swallowed(server_cfg, mock_server):
    bridge = _bridge(server_cfg)
    assert bridge.handle(r.orm("NW", "O1", **CHEST).encode()).ack_code == "AA"
    res = bridge.handle(r.orm("NW", "O2", **ABDOMEN).encode())
    bridge.close()
    assert res.ack_code == "AE" and res.issues[0].code == "205" and "Duplicate key identifier" in res.ack


def test_reused_accession_for_another_patient_does_not_replace_the_first_patients_report(server_cfg, mock_server):
    bridge = _bridge(server_cfg)
    reused = dict(CHEST, placer="ORD9", filler="FIL9")                  # accession ACC1 reused after a counter reset
    assert bridge.handle(r.orm("NW", "O1", **CHEST).encode()).ack_code == "AA"
    assert bridge.handle(r.oru("F", "R1", **CHEST, findings=["a"], impression="PATIENT A report").encode()).ack_code == "AA"
    other = r.oru("F", "R2", **reused, findings=["b"], impression="PATIENT B report") \
        .replace(r.PATIENT["ids"], "SYN999999^^^SYNTH_HOSP^MR").replace("NÚÑEZ^JOSÉ^ANTONIO", "OTHER^PAT")
    res = bridge.handle(other.encode())
    bridge.close()
    assert res.ack_code == "AE" and [d["conclusion"] for d in mock_server.store.all("DiagnosticReport")] == ["PATIENT A report"]


# ---- a PUT replaces the whole resource: what other messages supplied must survive -------------------------
HIS = {**O1, "filler": "", "accession": ""}          # the HIS knows only its placer number
RPT = {**O1, "placer": ""}                           # the reporting system echoes filler + accession


def test_placer_only_order_change_does_not_erase_filler_and_accession(server_cfg, mock_server):
    bridge = _bridge(server_cfg)
    for m in (r.orm("NW", "HIS1", **HIS), r.orm("SC", "RIS1", **O1, order_status="IP"), r.orm("XO", "HIS2", **HIS),
              r.oru("F", "RPT1", **RPT, findings=["a"], impression="b")):
        assert bridge.handle(m.encode()).ack_code == "AA"
    bridge.close()
    [sr] = mock_server.store.all("ServiceRequest")
    assert {i["value"] for i in sr["identifier"]} == {"ORD1001", "FIL5001", "ACC2001"}
    assert mock_server.store.all("DiagnosticReport")[0]["basedOn"] == [{"reference": f"ServiceRequest/{sr['id']}"}]


def test_late_new_order_adds_its_placer_number_to_the_order_the_report_created(server_cfg, mock_server):
    bridge = _bridge(server_cfg)
    assert bridge.handle(r.oru("F", "RPT1", **RPT, findings=["a"], impression="b").encode()).ack_code == "AA"
    assert bridge.handle(r.orm("NW", "RIS1", **O1).encode()).ack_code == "AA"
    [sr] = mock_server.store.all("ServiceRequest")
    assert "ORD1001" in {i["value"] for i in sr["identifier"]}
    assert bridge.handle(r.orm("CA", "HIS1", **HIS, orc_only=True).encode()).ack_code == "AA"      # the HIS cancels by placer
    bridge.close()


def test_status_change_does_not_move_authored_on(server_cfg, mock_server):
    """R4 authoredOn = "when the request transitioned to being actionable"; the IG maps ORC-9 only when ORC-1 = NW."""
    bridge = _bridge(server_cfg)
    assert bridge.handle(sample_bytes("orm_o01_new.hl7")).ack_code == "AA"
    placed = mock_server.store.all("ServiceRequest")[0]["authoredOn"]
    sc = sample_bytes("orm_o01_status_completed.hl7").replace(b"||CM||^^^^^R||20260915083000|", b"||CM||^^^^^R||20260915103000|")
    assert b"20260915103000" in sc and bridge.handle(sc).ack_code == "AA"
    bridge.close()
    [sr] = mock_server.store.all("ServiceRequest")
    assert sr["status"] == "completed" and sr["authoredOn"] == placed


# ---- the order or report found by its numbers must belong to this message's patient ------------------------
OTHER_PATIENT = ("SYN555555^^^SYNTH_HOSP^MR", "OTHER^PAT")


def _for_other_patient(message: str) -> str:
    return message.replace(r.PATIENT["ids"], OTHER_PATIENT[0]).replace("NÚÑEZ^JOSÉ^ANTONIO", OTHER_PATIENT[1])


def test_report_with_the_same_numbers_for_another_patient_is_refused(server_cfg, mock_server):
    """Every order number matches, but PID-3 is someone else: writing it would put patient B's report on A's order."""
    bridge = _bridge(server_cfg)
    assert bridge.handle(r.orm("NW", "O1", **O1).encode()).ack_code == "AA"
    assert bridge.handle(r.oru("F", "R1", **O1, findings=["a"], impression="PATIENT A report").encode()).ack_code == "AA"
    res = bridge.handle(_for_other_patient(r.oru("C", "R2", **O1, findings=["b"], impression="PATIENT B report")).encode())
    bridge.close()
    assert res.ack_code == "AE" and "another patient" in str(res.issues[0])
    assert [d["conclusion"] for d in mock_server.store.all("DiagnosticReport")] == ["PATIENT A report"]


def test_order_placed_before_an_a40_merge_still_takes_the_surviving_patients_report(server_cfg, mock_server):
    """The order was placed on MRN SYN100999; A40 merged it into SYN100234; the report carries the survivor's MRN."""
    bridge = _bridge(server_cfg)
    old = {**r.PATIENT, "ids": "SYN100999^^^SYNTH_HOSP^MR"}
    assert bridge.handle(r.orm("NW", "O1", **O1).replace(r.PATIENT["ids"], old["ids"]).encode()).ack_code == "AA"
    assert bridge.handle(sample_bytes("adt_a40_merge.hl7")).ack_code == "AA"
    res = bridge.handle(r.oru("F", "R1", **O1, findings=["a"], impression="IMPRESSION: ok").encode())
    bridge.close()
    assert res.ack_code == "AA", [str(i) for i in res.issues]
    assert mock_server.store.all("DiagnosticReport")[0]["conclusion"] == "IMPRESSION: ok"
