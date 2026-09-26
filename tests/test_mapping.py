"""Each message type -> the FHIR resources and requests it must produce. Every bundle here is also
validated (fhir.resources R4B models + required bindings) by the to_bundle helper."""
import base64
import re

import pytest

from tests.conftest import SAMPLES, entry, resources, sample_bytes, to_bundle
from tools import ris_sim as r
from v2fhir.convert import UnsupportedMessage, convert
from v2fhir.errors import MappingError
from v2fhir.hl7.parser import parse, parse_bytes
from v2fhir.mapping.provenance import Z_SEGMENT_EXTENSION

O1 = dict(placer="ORD1001", filler="FIL5001", accession="ACC2001", procedure=r.CT_CHEST, modality="CT")


# ---- ADT -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("trigger", ["A01", "A04", "A08"])
def test_adt_patient_and_encounter_are_upserts(cfg, trigger):
    b = to_bundle(r.adt(trigger, "C1"), cfg)
    pe = entry(b, "Patient")
    assert pe["request"] == {"method": "PUT", "url": "Patient?identifier=http://example.org/fhir/sid/synth-hosp|SYN100234"}
    p = pe["resource"]
    assert p["name"][0] == {"use": "official", "family": "NÚÑEZ", "given": ["JOSÉ", "ANTONIO"]}
    assert p["gender"] == "male" and p["birthDate"] == "1968-04-12" and p["active"] is True
    assert [i["value"] for i in p["identifier"]] == ["SYN100234", "E77120"] and p["identifier"][0]["use"] == "usual"
    assert p["telecom"][0] == {"system": "phone", "value": "(210) 5550100", "use": "home"}
    assert p["address"][0]["city"] == "Sampleton" and p["meta"]["source"] == "urn:hl7v2:RIS_SIM:SYNTH_HOSP#C1"
    ee = entry(b, "Encounter")
    assert ee["request"]["method"] == "PUT" and ee["request"]["url"].endswith("|V900001")
    enc = ee["resource"]
    assert enc["class"]["code"] == "AMB" and enc["status"] == "in-progress" and enc["subject"]["reference"] == pe["fullUrl"]
    assert enc["participant"][0]["type"][0]["coding"][0]["code"] == "ATND"
    assert entry(b, "Practitioner")["request"]["ifNoneExist"] == "identifier=http://example.org/fhir/sid/synth-provider|1001"


def test_sex_table_0001(cfg):
    for code, gender in (("F", "female"), ("M", "male"), ("O", "other"), ("U", "unknown"), ("A", "other"), ("N", "unknown")):
        msg = r.adt("A04", "C1").replace("|19680412|M|", f"|19680412|{code}|")
        assert entry(to_bundle(msg, cfg), "Patient")["resource"]["gender"] == gender
    msg = r.adt("A04", "C1").replace("|19680412|M|", "|19680412|X|")
    conv = convert(parse(msg), cfg)
    assert "gender" not in resources(conv.bundle, "Patient")[0] and any("table 0001" in str(w) for w in conv.warnings)


def test_repeated_pid3_chooses_the_configured_mrn_not_the_first(cfg):
    b = to_bundle(sample_bytes("adt_a04_v23_latin1.hl7"), cfg)
    pe = entry(b, "Patient")
    assert pe["request"]["url"].endswith("synth-hosp|SYN100555")          # 2nd repetition, SYNTH_HOSP
    assert pe["resource"]["name"][0]["family"] == "MUÑOZ"                       # Latin-1 decoded via MSH-18 8859/1
    assert "telecom" not in pe["resource"]                                      # PID-13 was "" (explicit null)


def test_mrn_choice_without_configured_authority(cfg):
    cfg.mrn_authorities = ()
    msg = r.adt("A04", "C1").replace(r.PATIENT["ids"], "P-1^^^OTHER^PI~M-2^^^OTHER2^MR")
    conv = convert(parse(msg), cfg)
    assert entry(conv.bundle, "Patient")["request"]["url"].endswith("|M-2")      # first MR, not first repetition
    msg = r.adt("A04", "C1").replace(r.PATIENT["ids"], "P-1^^^OTHER^PI")
    conv = convert(parse(msg), cfg)
    assert entry(conv.bundle, "Patient")["request"]["url"].endswith("|P-1") and any("no MR-typed" in str(w) for w in conv.warnings)


def test_missing_patient_identifier_is_an_ae(cfg):
    msg = r.adt("A04", "C1").replace(r.PATIENT["ids"], "")
    with pytest.raises(MappingError) as e:
        convert(parse(msg), cfg)
    assert e.value.ack_code == "AE" and e.value.issue.code == "101" and str(e.value.issue.location) == "PID[1]-3"


def test_a08_without_visit_number_updates_patient_only(cfg):
    msg = r.adt("A08", "C1").replace("V900001^^^SYNTH_HOSP^VN", "")
    conv = convert(parse(msg), cfg)
    assert not resources(conv.bundle, "Encounter") and any("PV1-19" in str(w) for w in conv.warnings)


def test_a40_merge_links_both_patients(cfg):
    b = to_bundle(sample_bytes("adt_a40_merge.hl7"), cfg)
    pats = {e["request"]["url"].split("|")[-1]: e for e in b["entry"] if e["resource"]["resourceType"] == "Patient"}
    survivor, old = pats["SYN100234"], pats["SYN100999"]
    assert survivor["request"]["method"] == old["request"]["method"] == "PUT"
    assert survivor["resource"]["link"] == [{"other": {"reference": old["fullUrl"]}, "type": "replaces"}]
    assert old["resource"]["link"] == [{"other": {"reference": survivor["fullUrl"]}, "type": "replaced-by"}]
    assert old["resource"]["active"] is False and old["resource"]["name"][0]["family"] == "NUNEZ"


def test_a40_merging_a_patient_into_itself_is_refused(cfg):
    msg = sample_bytes("adt_a40_merge.hl7").replace(b"SYN100999", b"SYN100234")
    with pytest.raises(MappingError):
        convert(parse(msg.decode()), cfg)


def test_unsupported_message_types(cfg):
    with pytest.raises(UnsupportedMessage) as e:
        convert(parse(r.adt("A03", "C1")), cfg)
    assert e.value.issue.code == "201"
    with pytest.raises(UnsupportedMessage) as e:
        convert(parse("MSH|^~\\&|A|B|C|D|2026||SIU^S12|1|P|2.5\r"), cfg)
    assert e.value.issue.code == "200"


# ---- orders ------------------------------------------------------------------------------------------
def test_orm_new_order(cfg):
    b = to_bundle(r.orm("NW", "C2", **O1, study_uid=r.STUDY_UID_1), cfg)
    se = entry(b, "ServiceRequest")
    # matched on ANY of its numbers (comma = OR), so a result that only echoes one of them still finds it
    assert se["request"] == {"method": "POST", "url": "ServiceRequest",
                             "ifNoneExist": "identifier=http://example.org/fhir/sid/synth-his|ORD1001,"
                                            "http://example.org/fhir/sid/synth-ris|FIL5001,http://example.org/fhir/sid/accession|ACC2001"}
    sr = se["resource"]
    assert sr["status"] == "active" and sr["intent"] == "order" and sr["priority"] == "routine"
    types = {i["type"]["coding"][0]["code"]: i for i in sr["identifier"]}
    assert types["PLAC"]["value"] == "ORD1001" and types["FILL"]["value"] == "FIL5001"
    assert types["ACSN"] == {"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "ACSN", "display": "Accession ID"}]},
                             "system": "http://example.org/fhir/sid/accession", "value": "ACC2001"}
    assert sr["code"]["coding"][0] == {"system": "http://www.ama-assn.org/go/cpt", "code": "71250", "display": "CT CHEST W/O CONTRAST"}
    assert sr["orderDetail"][0]["coding"][0] == {"system": "http://dicom.nema.org/resources/ontology/DCM", "code": "CT", "display": "Computed Tomography"}
    assert sr["reasonCode"][0]["coding"][0] == {"system": "http://hl7.org/fhir/sid/icd-10", "code": "R05.9", "display": "Cough, unspecified"}
    assert sr["occurrenceDateTime"] == "2026-09-15T10:00:00-05:00" and sr["authoredOn"] == "2026-09-15T08:30:00-05:00"
    assert sr["requester"]["reference"] == entry(b, "Practitioner")["fullUrl"] and sr["requester"]["display"] == "DR ORDERING A SAMPLE MD"
    assert sr["subject"]["reference"] == entry(b, "Patient")["fullUrl"] and entry(b, "Patient")["request"]["method"] == "POST"


@pytest.mark.parametrize("control, orc5, status", [
    ("NW", "", "active"), ("NW", "SC", "active"), ("SC", "IP", "active"), ("SC", "CM", "completed"), ("SC", "CA", "revoked"),
    ("SC", "HD", "on-hold"), ("XO", "", "active"), ("CA", "", "revoked"), ("CA", "SC", "revoked"), ("DC", "", "revoked"),
    ("OC", "", "revoked"), ("HD", "", "on-hold"), ("RL", "", "active"),
])
def test_order_status_from_orc1_and_orc5(cfg, control, orc5, status):
    b = to_bundle(r.orm(control, "C", **O1, order_status=orc5), cfg)
    se = entry(b, "ServiceRequest")
    assert se["resource"]["status"] == status
    assert se["request"]["method"] == ("POST" if control == "NW" else "PUT")


def test_bad_order_control_and_sc_without_status(cfg):
    with pytest.raises(MappingError) as e:
        convert(parse(r.orm("ZZ", "C", **O1)), cfg)
    assert e.value.issue.code == "103"
    with pytest.raises(MappingError) as e:
        convert(parse(r.orm("SC", "C", **O1)), cfg)
    assert "ORC-5" in str(e.value)


def test_orc_only_cancel_is_a_status_patch(cfg):
    b = to_bundle(sample_bytes("orm_o01_cancel_orc_only.hl7"), cfg)
    pe = b["entry"][0]
    assert pe["request"] == {"method": "PATCH", "url": "ServiceRequest?identifier=http://example.org/fhir/sid/synth-his|ORD1002,"
                                                        "http://example.org/fhir/sid/synth-ris|FIL5002"}
    assert pe["resource"]["parameter"][0]["part"] == [{"name": "type", "valueCode": "replace"},
                                                       {"name": "path", "valueString": "ServiceRequest.status"},
                                                       {"name": "value", "valueCode": "revoked"}]
    prov = entry(b, "Provenance")["resource"]
    # a PATCH entry has no resource in the bundle to point at, so Provenance references the order by identifier
    assert prov["target"] == [{"type": "ServiceRequest", "identifier": {
        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "PLAC", "display": "Placer Identifier"}]},
        "system": "http://example.org/fhir/sid/synth-his", "value": "ORD1002"}}]


def test_omi_o23_uses_ipc_and_tq1(cfg):
    b = to_bundle(sample_bytes("omi_o23_new_stat.hl7"), cfg)
    sr = entry(b, "ServiceRequest")["resource"]
    acc = next(i for i in sr["identifier"] if i["type"]["coding"][0]["code"] == "ACSN")
    assert acc["value"] == "ACC2101" and acc["system"] == "http://example.org/fhir/sid/synth-ris"
    assert sr["priority"] == "stat" and sr["occurrenceDateTime"] == "2026-09-16T09:30:00-05:00"
    assert sr["orderDetail"][0]["coding"][0]["code"] == "CT"
    assert sr["reasonCode"][0]["coding"][0]["code"] == "R10.9" and "premedicated" in sr["note"][0]["text"]
    assert entry(b, "Encounter")["resource"]["class"]["code"] == "EMER"


def test_same_provider_twice_in_one_message_is_one_entry(cfg):
    # ORC-12 and OBR-16 and PV1-7 are the same person: two conditional creates for one resource in one
    # transaction is an error on real servers.
    b = to_bundle(r.orm("NW", "C", **O1), cfg)
    assert len(resources(b, "Practitioner")) == 1


def test_provider_without_id_is_display_only(cfg):
    msg = r.orm("NW", "C", **O1).replace(r.ORDERING, "^NOID^NANCY")
    b = to_bundle(msg, cfg)
    assert not resources(b, "Practitioner")
    assert entry(b, "ServiceRequest")["resource"]["requester"] == {"display": "NANCY NOID"}


def test_order_without_any_number_is_an_ae(cfg):
    msg = r.orm("NW", "C", **O1).replace("ORD1001^SYNTH_HIS", "").replace("FIL5001^SYNTH_RIS", "").replace("ACC2001", "")
    with pytest.raises(MappingError) as e:
        convert(parse(msg), cfg)
    assert e.value.issue.code == "101"


# ---- results -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("obr25, status", [("P", "preliminary"), ("F", "final"), ("C", "corrected"), ("X", "cancelled"), ("I", "registered"),
                                           ("A", "partial")])
def test_oru_status_table_0123(cfg, obr25, status):
    b = to_bundle(r.oru(obr25, "R", **O1, findings=["x"], impression="y"), cfg)
    assert entry(b, "DiagnosticReport")["resource"]["status"] == status


def test_oru_diagnostic_report(cfg):
    b = to_bundle(r.oru("F", "R1", **O1, findings=r.FINDINGS, impression="IMPRESSION: nodule.", study_uid=r.STUDY_UID_1), cfg)
    de = entry(b, "DiagnosticReport")
    assert de["request"]["method"] == "PUT" and de["request"]["url"].startswith("DiagnosticReport?identifier=http://example.org/fhir/sid/synth-his|ORD1001,")
    dr = de["resource"]
    assert dr["basedOn"] == [{"reference": entry(b, "ServiceRequest")["fullUrl"]}]
    assert dr["imagingStudy"] == [{"reference": entry(b, "ImagingStudy")["fullUrl"]}]
    assert dr["conclusion"] == "IMPRESSION: nodule."
    assert dr["category"][0]["coding"][0] == {"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "CT"}
    assert dr["effectiveDateTime"] == "2026-09-15T10:12:00-05:00" and dr["issued"] == "2026-09-15T12:00:00-05:00"
    assert dr["resultsInterpreter"][0]["display"] == "DR RACHEL READER MD"
    text = base64.b64decode(dr["presentedForm"][0]["data"]).decode("utf-8")
    assert text.startswith("FINDINGS: Lungs: 6 mm") and text.endswith("IMPRESSION: nodule.")
    se = entry(b, "ServiceRequest")
    assert se["request"]["method"] == "POST" and se["resource"]["status"] == "completed"       # create-if-absent only
    study = entry(b, "ImagingStudy")
    assert study["request"]["ifNoneExist"] == f"identifier=urn:dicom:uid|urn:oid:{r.STUDY_UID_1}"
    assert study["resource"]["identifier"][1]["value"] == "ACC2001" and study["resource"]["modality"][0]["code"] == "CT"


def test_oru_formatted_text_escapes_and_coded_conclusion(cfg):
    b = to_bundle(sample_bytes("oru_r01_ft_escapes.hl7"), cfg)
    dr = entry(b, "DiagnosticReport")["resource"]
    text = base64.b64decode(dr["presentedForm"][0]["data"]).decode("utf-8")
    assert "EXAM: CT chest low-dose screening\n\nTECHNIQUE" in text           # \.br\ -> newline
    assert "T2^weighted" in text                                               # unescaped ^ in free text survives
    assert "(image 3|41)" in text                                              # \F\ -> |
    assert dr["conclusion"] == "IMPRESSION:\nLung-RADS 2: benign appearance. Annual screening in 12 months."   # \H\ \N\ dropped
    assert dr["conclusionCode"][0]["coding"][0]["code"] == "LR2"
    assert "synth-his|ORD1201," in entry(b, "DiagnosticReport")["request"]["url"]


def test_oru_without_impression_uses_whole_text_and_without_status_is_ae(cfg):
    msg = r.oru("F", "R", **O1, findings=["only findings"], impression="imp").replace("&IMP^Impression", "&GDT^Findings")
    assert entry(to_bundle(msg, cfg), "DiagnosticReport")["resource"]["conclusion"] == "only findings\nimp"
    with pytest.raises(MappingError) as e:
        convert(parse(r.oru("", "R", **O1, findings=["x"], impression="y")), cfg)
    assert "OBR-25" in str(e.value)
    with pytest.raises(MappingError) as e:
        convert(parse(r.oru("Q", "R", **O1, findings=["x"], impression="y")), cfg)
    assert e.value.issue.code == "103"


def test_invalid_study_uid_gives_no_imaging_study(cfg):
    conv = convert(parse(r.oru("F", "R", **O1, findings=["x"], impression="y", study_uid="1.2.abc")), cfg)
    assert not resources(conv.bundle, "ImagingStudy") and any("not a valid DICOM UID" in str(w) for w in conv.warnings)


# ---- provenance ----------------------------------------------------------------------------------------
def test_provenance_traces_msh10_and_keeps_z_segments(cfg):
    b = to_bundle(r.orm("NW", "RIS00002", **O1, study_uid=r.STUDY_UID_1), cfg)
    pe = entry(b, "Provenance")
    prov = pe["resource"]
    assert pe["request"]["method"] == "PUT" and pe["request"]["url"] == f"Provenance/{prov['id']}"
    assert prov["entity"][0]["what"]["identifier"] == {"system": "urn:hl7v2:RIS_SIM:SYNTH_HOSP", "value": "RIS00002"}
    assert {t["reference"] for t in prov["target"]} == {e["fullUrl"] for e in b["entry"] if e is not pe}
    assert prov["extension"] == [{"url": Z_SEGMENT_EXTENSION, "valueString": f"ZDS|{r.STUDY_UID_1}^RIS_SIM^Application^DICOM"}]
    assert prov["activity"]["coding"][0]["code"] == "O01" and prov["occurredDateTime"] == "2026-09-15T08:30:00-05:00"
    assert b["identifier"] == {"system": "urn:hl7v2:RIS_SIM:SYNTH_HOSP", "value": "RIS00002"}
    # deterministic: same message -> same Provenance id and same bundle
    assert to_bundle(r.orm("NW", "RIS00002", **O1, study_uid=r.STUDY_UID_1), cfg) == b


def test_every_sample_converts_and_validates(cfg):
    for path in sorted(SAMPLES.glob("*.hl7")):
        to_bundle(path.read_bytes(), cfg)


def test_second_group_with_the_same_order_number_is_not_dropped_silently(cfg):
    """Two ORC/OBR groups sharing one placer number used to collapse into one ServiceRequest with no warning."""
    msg = r.orm("NW", "C1", **O1)
    segs = msg.strip("\r").split("\r")
    orc = next(s for s in segs if s.startswith("ORC"))
    obr2 = next(s for s in segs if s.startswith("OBR")).replace(r.CT_CHEST, "74150^CT ABDOMEN W/O CONTRAST^C4").replace("OBR|1|", "OBR|2|", 1)
    conv = convert(parse(msg + orc + "\r" + obr2 + "\r"), cfg)
    assert len(resources(conv.bundle, "ServiceRequest")) == 1
    assert any("second ORC/OBR group for order ORD1001" in str(w) for w in conv.warnings)


def test_omi_ipc_segment_with_the_study_uid_is_kept_on_the_provenance(cfg):
    """IPC-3 (Study Instance UID) has no ServiceRequest element; it must not vanish."""
    raw = sample_bytes("omi_o23_new_stat.hl7")
    ipc = next(seg for seg in raw.decode("utf-8").replace("\n", "\r").split("\r") if seg.startswith("IPC"))
    prov = resources(to_bundle(raw, cfg), "Provenance")[0]
    assert ipc.split("|")[3].split("^")[0] in [x["valueString"].split("|")[3].split("^")[0] for x in prov["extension"] if x["valueString"].startswith("IPC")]


def test_discharge_before_admit_keeps_the_period_valid(cfg):
    """FHIR per-1: Period.start <= Period.end. A keying error (discharge an hour before admit) must not produce an
    invalid Encounter; the end is dropped with a warning and the encounter is still finished."""
    raw = re.sub(rb"(PV1\|[^\r\n]*\|200609150800\|)(?=[\r\n])", rb"\g<1>200609150700", sample_bytes("adt_a04_v23_latin1.hl7"))
    assert b"200609150700" in raw
    conv = convert(parse_bytes(raw), cfg)
    enc = resources(conv.bundle, "Encounter")[0]
    assert "end" not in enc["period"] and enc["status"] == "finished" and any("PV1-45" in str(w) for w in conv.warnings)


@pytest.mark.parametrize("sample", ["orm_o01_new.hl7", "orm_o01_status_completed.hl7"])
def test_order_detail_only_with_a_code(cfg, sample):
    """FHIR prr-1: ServiceRequest.orderDetail requires ServiceRequest.code."""
    raw = re.sub(rb"(OBR\|1\|[^|]*\|[^|]*\|)71250\^CT CHEST W/O CONTRAST\^C4\|", rb"\1|", sample_bytes(sample))
    assert b"71250" not in raw
    for sr in resources(to_bundle(raw, cfg), "ServiceRequest"):
        assert "orderDetail" not in sr or "code" in sr


def test_conclusion_stays_within_the_fhir_string_limit(cfg):
    """A FHIR string is at most 1 MB. A 2 MB report without an impression line was copied whole into
    conclusion; presentedForm (base64Binary) is where the full text belongs."""
    lines = ["Line %05d of a very long report without an impression section." % i for i in range(40_000)]
    conv = convert(parse(r.oru("F", "BIG1", **O1, findings=lines, impression="")), cfg)
    dr = resources(conv.bundle, "DiagnosticReport")[0]
    assert len(dr["conclusion"].encode("utf-8")) <= 1024 * 1024 and dr["conclusion"].endswith("full report in presentedForm]")
    assert base64.b64decode(dr["presentedForm"][0]["data"]).decode("utf-8").count("Line ") == len(lines)


def test_order_comment_keeps_every_repetition_and_a_stray_caret(cfg):
    """NTE-3 is FT and repeats; only the first component of the first repetition used to survive."""
    msg = r.orm("NW", "N1", **O1) + "NTE|1||History: prior lobectomy^ right side~Allergy: iodinated contrast\r"
    sr = resources(to_bundle(msg, cfg), "ServiceRequest")[0]
    assert sr["note"] == [{"text": "History: prior lobectomy^ right side\nAllergy: iodinated contrast"}]
