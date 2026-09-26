"""v2 tables -> FHIR code systems. Every translation the bridge makes is in this file.

Each map is (v2 table) -> (FHIR element's value set). Where the HL7 v2-to-FHIR Implementation Guide has a
ConceptMap, these follow it; deviations are called out here and in docs/MAPPING.md. A code that is not
in a map is never guessed: the element is left out and a warning goes back in the ACK / log.
"""
from __future__ import annotations

V2 = "http://terminology.hl7.org/CodeSystem/v2-"          # + table number, e.g. v2-0203
V3_ACTCODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
V3_NULLFLAVOR = "http://terminology.hl7.org/CodeSystem/v3-NullFlavor"
V3_PARTICIPATION = "http://terminology.hl7.org/CodeSystem/v3-ParticipationType"
PROVENANCE_PARTICIPANT = "http://terminology.hl7.org/CodeSystem/provenance-participant-type"
DICOM_DCM = "http://dicom.nema.org/resources/ontology/DCM"
SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
DICOM_UID_SYSTEM = "urn:dicom:uid"

# ---- 0001 Administrative sex -> AdministrativeGender (required binding) ------------------
SEX_0001 = {"M": "male", "F": "female", "O": "other", "U": "unknown", "A": "other", "N": "unknown"}

# ---- 0200 Name type -> HumanName.use ------------------------------------------------------
# A (v2.5.1 Alias), T (Indigenous/Tribal/Community name) and the other codes the IG leaves unmatched get no use.
# S (v2.3-2.5.1 "Coded pseudo-name to ensure anonymity") -> anonymous is a deviation: the IG leaves S unmatched.
NAME_USE_0200 = {"L": "official", "D": "usual", "M": "maiden", "N": "nickname", "S": "anonymous",
                 "TEMP": "temp", "NAV": "temp", "BAD": "old"}

# ---- 0190 Address type -> Address.use / Address.type ------------------------------------
ADDRESS_USE_0190 = {"H": "home", "B": "work", "O": "work", "C": "temp", "BA": "old", "M": "home"}
ADDRESS_TYPE_0190 = {"M": "postal"}

# ---- 0201 Telecom use / 0202 Telecom equipment -> ContactPoint --------------------------
TELECOM_USE_0201 = {"PRN": "home", "ORN": "home", "VHN": "home", "WPN": "work"}
TELECOM_SYSTEM_0202 = {"PH": "phone", "FX": "fax", "CP": "phone", "BP": "pager", "INTERNET": "email", "X.400": "email",
                       "MD": "other", "TDD": "other", "TTY": "other"}

# ---- 0203 Identifier type: FHIR uses the v2 table itself as the code system ---------------
IDENTIFIER_TYPE_SYSTEM = V2 + "0203"
IDENTIFIER_TYPE_DISPLAY = {
    "MR": "Medical record number", "PI": "Patient internal identifier", "PT": "Patient external identifier",
    "AN": "Account number", "VN": "Visit number", "NPI": "National provider identifier", "PRN": "Provider number",
    "PLAC": "Placer Identifier", "FILL": "Filler Identifier", "ACSN": "Accession ID", "DL": "Driver's license number",
    "EI": "Employee number", "PN": "Person number",
}

# ---- 0004 Patient class -> Encounter.class (v3 ActCode; Coding is 1..1 in R4) ------------
PATIENT_CLASS_0004 = {"E": ("EMER", "emergency"), "I": ("IMP", "inpatient encounter"), "O": ("AMB", "ambulatory"),
                      "P": ("PRENC", "pre-admission"), "R": ("AMB", "ambulatory"), "B": ("IMP", "inpatient encounter")}

# ---- 0119 Order control -> ServiceRequest.status -------------------------------------------
# NW/XO/SC/OK/RL/OR say "look at ORC-5 if present"; cancels, discontinues and holds decide on their own.
ORDER_CONTROL_0119 = {
    "NW": "active", "OK": "active", "XO": "active", "XX": "active", "SC": None, "RL": "active", "OR": "active",
    "CA": "revoked", "CR": "revoked", "OC": "revoked", "DC": "revoked", "DR": "revoked", "OD": "revoked",
    "HD": "on-hold", "OH": "on-hold",
}
ORDER_CONTROL_DEFERS_TO_ORC5 = {"NW", "OK", "XO", "XX", "SC", "RL", "OR"}
ORDER_CONTROL_IS_NEW = {"NW"}      # conditional create; everything else changes an existing order

# ---- 0038 Order status -> ServiceRequest.status ---------------------------------------------
ORDER_STATUS_0038 = {"A": "active", "IP": "active", "SC": "active", "CM": "completed", "CA": "revoked", "DC": "revoked",
                     "RP": "revoked", "HD": "on-hold", "ER": "entered-in-error"}

# ---- 0123 Result status (OBR-25) -> DiagnosticReport.status ---------------------------------
# C -> "corrected". R4's "corrected" is a specialisation of "amended" (amended > corrected | appended),
# so anything that looks for amended-or-below finds it. Table 0123 defines C as "correction to results",
# the same meaning as R4 corrected ("modified after final to correct an error"). Change this line if a consumer wants "amended".
# R ("results stored; not yet verified") is partial, as in the v2-to-FHIR IG: R4 preliminary means *verified* early results.
# A -> partial is a deviation (the IG leaves A unmatched). Codes outside table 0123 (D, Y, Z...) are refused with AE 103.
RESULT_STATUS_0123 = {"O": "registered", "I": "registered", "S": "registered", "A": "partial", "P": "preliminary",
                      "R": "partial", "F": "final", "C": "corrected", "X": "cancelled"}
# A result tells us the order was done, but never un-cancels or re-opens it (see mapping/results.py).
RESULT_STATUS_TO_ORDER = {"F": "completed", "C": "completed", "X": "revoked"}

# ---- 0027 Priority (TQ1-9, OBR-27.6, ORC-7.6) -> ServiceRequest.priority --------------------
PRIORITY_0027 = {"S": "stat", "A": "asap", "R": "routine", "T": "urgent", "P": "urgent"}

# ---- 0074 Diagnostic service section (OBR-24) -> DICOM modality, where unambiguous ----------
# Radiology senders often put a DICOM modality (MR, US, PT...) in OBR-24; those become the modality, not the category.
MODALITY_FROM_0074 = {"CT": "CT", "NMR": "MR", "MR": "MR", "US": "US", "NMS": "NM", "MG": "MG", "PT": "PT", "XA": "XA", "RF": "RF",
                      "DX": "DX", "CR": "CR", "NM": "NM"}
# Table 0074 itself (THO v2-0074). DiagnosticReport.category only ever carries one of these.
SERVICE_SECTION_0074 = frozenset({
    "AU", "BG", "BLB", "CG", "CUS", "CTH", "CT", "CH", "CP", "EC", "EN", "GE", "HM", "IMG", "ICU", "IMM", "LAB", "MB", "MCB", "MYC",
    "NMS", "NMR", "NRS", "OUS", "OT", "OTH", "OSL", "PAR", "PHR", "PAT", "PT", "PHY", "PF", "RAD", "RX", "RUS", "RC", "RT", "SR", "SP",
    "TX", "VUS", "VR", "URN", "XRC"})
# OBR-24 values read as DICOM modalities that are not table 0074 codes (PT is 0074 "Physical Therapy"; here it is PET).
DICOM_NOT_0074 = frozenset(MODALITY_FROM_0074) - {"CT", "NMR", "NMS"}
DICOM_MODALITY_DISPLAY = {"CT": "Computed Tomography", "MR": "Magnetic Resonance", "US": "Ultrasound", "NM": "Nuclear Medicine",
                          "MG": "Mammography", "PT": "Positron emission tomography", "XA": "X-Ray Angiography", "RF": "Radio Fluoroscopy",
                          "DX": "Digital Radiography", "CR": "Computed Radiography"}

# ---- 0396 Coding system -> URI -------------------------------------------------------------
CODING_SYSTEM_0396 = {
    "LN": LOINC, "LOINC": LOINC,
    "SCT": SNOMED, "SNM": SNOMED, "SNM3": SNOMED, "SNOMED": SNOMED, "SNOMED-CT": SNOMED,
    "C4": "http://www.ama-assn.org/go/cpt", "CPT": "http://www.ama-assn.org/go/cpt", "CPT4": "http://www.ama-assn.org/go/cpt",
    "I10": "http://hl7.org/fhir/sid/icd-10", "I10C": "http://hl7.org/fhir/sid/icd-10-cm", "ICD10CM": "http://hl7.org/fhir/sid/icd-10-cm",
    "I9C": "http://hl7.org/fhir/sid/icd-9-cm", "I9CDX": "http://hl7.org/fhir/sid/icd-9-cm",
    "DCM": DICOM_DCM, "RADLEX": "http://radlex.org", "RID": "http://radlex.org",
    "HL70074": V2 + "0074",
}

# ---- Radiology report OBX-3 conventions -> which lines are the impression ------------------
IMPRESSION_CODES = {"IMP", "&IMP", "19005-8"}          # 19005-8 = LOINC Radiology Imaging study [Impression]
TEXT_VALUE_TYPES = {"TX", "FT", "ST"}
CODED_VALUE_TYPES = {"CE", "CWE", "CNE"}

IMAGING_CATEGORY = {"coding": [{"system": SNOMED, "code": "363679005", "display": "Imaging"}]}
