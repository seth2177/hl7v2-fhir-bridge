"""A simulated RIS / reporting system that produces synthetic radiology HL7 v2 traffic.

Every person, identifier, facility and UID here is invented. MRNs use the SYNTH_ prefix, phone numbers the
fictional 555-01xx range, Study UIDs the 2.25 (UUID-derived) root that no organisation owns.
"""
from __future__ import annotations

import uuid

CR = "\r"


def seg(*fields: str) -> str:
    return "|".join(fields)


def message(*segments: str) -> str:
    return CR.join(segments) + CR


def msh(msg_type: str, control_id: str, when: str = "20260915083000", version: str = "2.5.1", app: str = "RIS_SIM",
        facility: str = "SYNTH_HOSP", charset: str = "UNICODE UTF-8") -> str:
    fields = ["MSH", "^~\\&", app, facility, "V2FHIR", "BRIDGE", when, "", msg_type, control_id, "P", version]
    if charset:
        fields += ["", "", "", "", "", charset]
    return "|".join(fields)


# ---- the synthetic patient and staff -----------------------------------------------------------
PATIENT = {
    "ids": "SYN100234^^^SYNTH_HOSP^MR~E77120^^^SYNTH_EMPI^PI",
    "name": "NÚÑEZ^JOSÉ^ANTONIO^^^^L",
    "dob": "19680412",
    "sex": "M",
    "address": "100 Example St^Apt 4^Sampleton^TX^78000^USA^H",
    "phone": "^PRN^PH^^1^210^5550100~^NET^Internet^jose.nunez@example.org",
}
ORDERING = "1001^SAMPLE^ORDERING^A^^DR^MD^^SYNTH_PRV"
RADIOLOGIST = "2002&READER&RACHEL&&&DR&MD&&SYNTH_PRV"
STUDY_UID_1 = "2.25." + str(uuid.uuid5(uuid.NAMESPACE_OID, "hl7v2-fhir-bridge demo study 1").int)
STUDY_UID_2 = "2.25." + str(uuid.uuid5(uuid.NAMESPACE_OID, "hl7v2-fhir-bridge demo study 2").int)

CT_CHEST = "71250^CT CHEST W/O CONTRAST^C4"
MR_BRAIN = "70551^MRI BRAIN W/O CONTRAST^C4"


def pid(name: str = PATIENT["name"], ids: str = PATIENT["ids"]) -> str:
    return seg("PID", "1", "", ids, "", name, "", PATIENT["dob"], PATIENT["sex"], "", "", PATIENT["address"], "", PATIENT["phone"])


def pv1(visit: str = "V900001^^^SYNTH_HOSP^VN", patient_class: str = "O") -> str:
    return seg("PV1", "1", patient_class, "RAD^CT1^01^SYNTH_HOSP", "", "", "", ORDERING, "", "", "", "", "", "", "", "", "", "", "", visit)


def adt(trigger: str, control_id: str, name: str = PATIENT["name"], when: str = "20260915080000") -> str:
    return message(msh(f"ADT^{trigger}^ADT_A01", control_id, when), seg("EVN", trigger, when), pid(name), pv1())


def orm(control: str, control_id: str, placer: str, filler: str, accession: str, procedure: str, modality: str, *,
        order_status: str = "", study_uid: str = "", when: str = "20260915083000", orc_only: bool = False,
        scheduled: str = "202609151000-0500") -> str:
    orc = seg("ORC", control, f"{placer}^SYNTH_HIS", f"{filler}^SYNTH_RIS", "", order_status, "", "^^^^^R", "", when, "", "", ORDERING)
    if orc_only:
        return message(msh("ORM^O01^ORM_O01", control_id, when), orc)
    obr = seg("OBR", "1", f"{placer}^SYNTH_HIS", f"{filler}^SYNTH_RIS", procedure, "", "", "", "", "", "", "", "", "", "", "", ORDERING, "",
              accession, "REQ" + accession[3:], "SPS" + accession[3:], "", "", "", modality, "", "", "^^^^^R", "", "", "",
              "R05.9^Cough, unspecified^I10", "", "", "", "", scheduled)
    segs = [msh("ORM^O01^ORM_O01", control_id, when), pid(), pv1(), orc, obr]
    if study_uid:
        segs.append(seg("ZDS", f"{study_uid}^RIS_SIM^Application^DICOM"))
    return message(*segs)


def oru(result_status: str, control_id: str, placer: str, filler: str, accession: str, procedure: str, modality: str,
        findings: list[str], impression: str, *, study_uid: str = "", when: str = "20260915120000") -> str:
    obr = seg("OBR", "1", f"{placer}^SYNTH_HIS", f"{filler}^SYNTH_RIS", procedure, "", "", "202609151012-0500", "", "", "", "", "", "", "", "",
              ORDERING, "", accession, "", "", "", when + "-0500", "", modality, result_status, "", "", "", "", "", "", RADIOLOGIST)
    segs = [msh("ORU^R01^ORU_R01", control_id, when), pid(), pv1(),
            seg("ORC", "RE", f"{placer}^SYNTH_HIS", f"{filler}^SYNTH_RIS", "", "CM"), obr]
    for i, line in enumerate(findings, 1):
        segs.append(seg("OBX", str(i), "TX", "&GDT^Findings", "1", line, "", "", "", "", "", result_status))
    segs.append(seg("OBX", str(len(findings) + 1), "TX", "&IMP^Impression", "1", impression, "", "", "", "", "", result_status))
    if study_uid:
        segs.append(seg("ZDS", f"{study_uid}^RIS_SIM^Application^DICOM"))
    return message(*segs)


FINDINGS = ["FINDINGS: Lungs: 6 mm solid nodule in the right upper lobe (series 3, image 42).",
            "No consolidation or effusion. Heart size normal. No mediastinal adenopathy."]


def demo_script() -> list[tuple[str, str]]:
    """(step description, message) in the order a RIS and a reporting system would send them."""
    o1 = dict(placer="ORD1001", filler="FIL5001", accession="ACC2001", procedure=CT_CHEST, modality="CT")
    o2 = dict(placer="ORD1002", filler="FIL5002", accession="ACC2002", procedure=MR_BRAIN, modality="NMR")
    return [
        ("register outpatient", adt("A04", "RIS00001")),
        ("new CT order", orm("NW", "RIS00002", **o1, study_uid=STUDY_UID_1)),
        ("exam completed (SC/CM)", orm("SC", "RIS00003", **o1, order_status="CM", study_uid=STUDY_UID_1)),
        ("preliminary report", oru("P", "RPT00001", **o1, findings=FINDINGS, study_uid=STUDY_UID_1,
                                   impression="IMPRESSION: 6 mm right upper lobe nodule. Preliminary read.")),
        ("final report", oru("F", "RPT00002", **o1, findings=FINDINGS, study_uid=STUDY_UID_1,
                             impression="IMPRESSION: 6 mm right upper lobe nodule. Follow-up CT in 6-12 months (Fleischner).")),
        ("corrected report", oru("C", "RPT00003", **o1, study_uid=STUDY_UID_1,
                                 findings=[FINDINGS[0].replace("upper", "lower"), FINDINGS[1]],
                                 impression="IMPRESSION: CORRECTED: nodule is in the right LOWER lobe. Follow-up CT in 6-12 months.")),
        ("name change (A08)", adt("A08", "RIS00004", name="NÚÑEZ-GARCÍA^JOSÉ^ANTONIO^^^^L")),
        ("second order: MR brain", orm("NW", "RIS00005", **o2, study_uid=STUDY_UID_2)),
        ("cancel second order (ORC only)", orm("CA", "RIS00006", **o2, orc_only=True)),
    ]
