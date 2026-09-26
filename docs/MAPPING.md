# Mapping: HL7 v2 → FHIR R4

Every field the bridge reads, where it lands in FHIR, and every code translation. The code is table-driven:
code-to-code maps live in [`v2fhir/mapping/tables.py`](../v2fhir/mapping/tables.py), and each mapping module
starts with a docstring that matches this page.

**Versions.** Input: HL7 v2.3, 2.3.1, 2.4, 2.5, 2.5.1 (configurable). Output: FHIR R4 (4.0.1) JSON. Validation
uses the `fhir.resources` R4B models, because the current release ships R4B, not R4. R4B did not change any
resource written here: Patient, Encounter, Practitioner, ServiceRequest, DiagnosticReport, ImagingStudy,
Provenance, Parameters and Bundle.

**Reference.** The mapping follows the conventions of the HL7 *v2-to-FHIR* Implementation Guide (segment →
resource, data type → data type, v2 table → code system). The IG leaves matching and identity to the
implementer, and so does this bridge in its own way. Every place where I knowingly differ from the IG is listed
under [Deviations and decisions](#deviations-and-decisions).

---

## 1. Messages → resources → requests

| Message | Resources | How each is written |
|---|---|---|
| ADT^A01 / A04 / A08 | Patient, Encounter, Practitioner, Provenance | Patient **upsert**, Encounter **upsert** (ADT is the source of truth for demographics); Practitioner create-if-absent |
| ADT^A40 | Patient ×2, Provenance | both **upsert**: survivor gets `link.type = replaces`, the retired one `active=false` + `link.type = replaced-by` |
| ORM^O01, OMI^O23 | ServiceRequest, Patient, Encounter, Practitioner, Provenance | NW → ServiceRequest **create-if-absent**; other ORC-1 with PID+OBR → **upsert**; ORC-only → **PATCH** `status`. Patient/Encounter create-if-absent |
| ORU^R01 | DiagnosticReport, ServiceRequest, ImagingStudy, Patient, Encounter, Practitioner, Provenance | DiagnosticReport **upsert**; ServiceRequest, ImagingStudy, Patient, Encounter, Practitioner **create-if-absent** |
| anything else | none | AA with a warning (`unsupported_messages = "ack"`) or AR 200/201 (`"reject"`) |

Request forms (all inside one `transaction` Bundle, all-or-nothing):

| Name | FHIR request | Meaning |
|---|---|---|
| create-if-absent | `POST Type` + `ifNoneExist: identifier=…` | make sure it exists; a replay matches and changes nothing |
| upsert | `PUT Type?identifier=…` | this message is authoritative: create or replace (for ServiceRequest and DiagnosticReport the order numbers the server already holds are kept, see §4) |
| patch | `PATCH Type?identifier=…` with a FHIRPath Patch `Parameters` | change one element; 404 if absent |
| put-by-id | `PUT Provenance/v2-<sha256(app\|facility\|MSH-10\|content hash)>` | Provenance has no identifier to search on; the content hash (MSH-7 ignored) gives a reused control id its own Provenance |

`identifier=a|1,b|2` means *any of* (comma is OR in FHIR search). Orders and reports are matched on every
order number the message carries (see §4). Token values are FHIR-escaped (`\|`, `\,`, `\$`, `\\`) and
percent-encoded.

Inside a bundle, entries reference each other by `urn:uuid:` fullUrls: UUIDv5 of the resource's key, so the same
message always produces the same bundle. The server resolves each conditional request first, then rewrites
the references to real ids.

## 2. Segments and fields

### MSH (message header)

| Field | Use |
|---|---|
| MSH-1, MSH-2 | delimiters (any printable punctuation; trailing encoding chars may be omitted) |
| MSH-3, MSH-4 | sending application / facility → `Provenance.agent[author].who.display`, part of `meta.source` and of the MSH-10 identifier system |
| MSH-5, MSH-6 | echoed as the ACK's MSH-3/4 |
| MSH-7 | `Provenance.occurredDateTime` (ignored in the duplicate check) |
| MSH-9 | message type / trigger / structure → dispatch; trigger → `Provenance.activity` (v2-0003) |
| MSH-10 | control id → `Provenance.entity.what.identifier`, `Bundle.identifier`, `meta.source` fragment, dedupe key, ACK MSA-2 |
| MSH-11 | processing id: the first component must be in `accepted_processing_ids` (default P), else AR 202; empty → AR 101. Echoed in the ACK |
| MSH-12 | version; must be in `accepted_versions` or AR 203 |
| MSH-18 | character set (table 0211), see §5 |

Every resource gets `meta.source = urn:hl7v2:<MSH-3>:<MSH-4>#<MSH-10>` (percent-encoded parts).

### PID → Patient

| Field | FHIR | Notes |
|---|---|---|
| PID-3 (CX, repeating) | `identifier[]` | every repetition kept; the one chosen as the MRN gets `use = usual` and is the match key (§4) |
| PID-5 (XPN, repeating) | `name[]` | family = XPN-1.1, given = XPN-2 + XPN-3, prefix XPN-5, suffix XPN-4 + XPN-6 (degree), use from XPN-7 (table 0200) |
| PID-7 | `birthDate` | date part only, any precision (YYYY, YYYY-MM, YYYY-MM-DD) |
| PID-8 | `gender` | table 0001; unknown code → omitted + warning |
| PID-11 (XAD) | `address[]` | table 0190 |
| PID-13 / PID-14 (XTN) | `telecom[]` | use home / work when XTN-2 is empty; table 0201/0202 (an unknown XTN-2 leaves use out, an unknown XTN-3 gives system other); e-mail detected |
| PID-29 / PID-30 | `deceasedDateTime` / `deceasedBoolean` | |
| — | `active = true` | `false` only for the retired record of an A40 |

Not mapped on purpose: PID-19 (SSN) and PID-6 (mother's maiden name). A radiology workflow doesn't need
them, and every copy is another place to leak them. Not mapped for scope: PID-2 and PID-4 (deprecated),
PID-10 and PID-22 (race and ethnicity: US Core extensions), PID-15 (language), PD1, NK1, AL1, IN1, GT1.

### PV1 → Encounter

| Field | FHIR | Notes |
|---|---|---|
| PV1-19 (CX) | `identifier` (type VN) | the match key. **No PV1-19 means no Encounter**, with a warning: there is nothing stable to match on |
| PV1-2 | `class` | table 0004 → v3 ActCode; unmapped → `v3-NullFlavor#UNK` (class is 1..1 in R4) |
| PV1-3 (PL) | `location[].location.display` | point of care^room^bed^facility as text; no Location resources |
| PV1-7 / PV1-8 / PV1-17 | `participant[]` ATND / REF / ADM | Practitioner (§ XCN) |
| PV1-44 / PV1-45 | `period.start` / `period.end` | |
| — | `status` | `finished` if PV1-45 is present, else `in-progress` |

### MRG (ADT^A40)

| Field | FHIR |
|---|---|
| MRG-1 (CX, repeating) | the retired Patient's `identifier[]`; MRN chosen as for PID-3 |
| MRG-7 | the retired Patient's `name` (falls back to PID-5) |
| — | retired: `active=false`, `link = {other: survivor, type: replaced-by}`; survivor: `link = {other: retired, type: replaces}` |

MRG-1 naming the same patient as PID-3 is an AE (205).

Later A01/A04/A08 keep the merge: the bridge reads the current Patient first (needs the FHIR server), carries its
`link` entries into the PUT, and an ADT that still carries the merged-away MRN is applied as create-if-absent with
a warning, so it can't re-activate that record. Orders and results that still carry the old MRN attach to the
retired record; clients follow `link.type = replaced-by`.

### ORC / OBR / TQ1 / IPC / DG1 / NTE → ServiceRequest

| Field | FHIR | Notes |
|---|---|---|
| ORC-1 | `status` (with ORC-5), request form (§1) | table 0119, see §3 |
| ORC-2 (else OBR-2) | `identifier` type **PLAC** | EI: entity id ^ namespace ^ universal id ^ type |
| ORC-3 (else OBR-3) | `identifier` type **FILL** | |
| OBR-18 (ORM) / IPC-1 (OMI) | `identifier` type **ACSN** | IHE Scheduled Workflow puts the accession number in OBR-18 (ORM) and IPC-1 (OMI) |
| ORC-5 | `status` | table 0038 |
| ORC-9 | `authoredOn` | only when ORC-1 = NW (v2-to-FHIR IG); a later status change keeps the stored value |
| ORC-12 (else OBR-16) | `requester` | Practitioner |
| OBR-4 | `code` | CE/CWE |
| IPC-5 (else OBR-24) | `orderDetail[]` | modality as a DICOM (DCM) coding, e.g. `CT`, `MR`; see note |
| TQ1-9 / OBR-27.6 / ORC-7.6 | `priority` | table 0027 |
| TQ1-7 / OBR-36 / OBR-27.4 | `occurrenceDateTime` | |
| OBR-31, DG1-3 | `reasonCode[]` | |
| NTE-3 | `note[].text` | |
| — | `intent = order`, `category = SNOMED 363679005 Imaging` | |
| ZDS-1 / IPC-3 | not on the ServiceRequest | the ZDS and IPC segments are kept verbatim on the Provenance; an ImagingStudy is created only when the ORU carries ZDS-1 |

R4 ServiceRequest has no modality element. `orderDetail` ("additional order information") carries it as a
DICOM coding, and `ImagingStudy.modality` gets it properly once a study exists.

### ORU^R01: OBR / OBX / ZDS → DiagnosticReport, ImagingStudy

| Field | FHIR | Notes |
|---|---|---|
| OBR-2/-3/-18 | `identifier[]` (same as the order), `basedOn` → the ServiceRequest | report and order are matched on the same numbers |
| OBR-4 | `code` | required (1..1): empty → AE 101 |
| OBR-7 | `effectiveDateTime` | |
| OBR-22 | `issued` | FHIR `instant`: needs a time; date-only → omitted + warning |
| OBR-24 | `category` (v2-0074), ImagingStudy `modality` | category = OBR-24 when it is a table 0074 code, else `RAD` with the sent value in `category.text` (warning unless it is a DICOM modality) |
| OBR-25 | `status` | table 0123; empty → AE 101, unknown → AE 103 |
| OBR-32 (NDL) | `resultsInterpreter[]` | NDL component 1 is a CNN whose parts are **sub**components (`id&family&given…`) |
| OBX (TX/FT/ST) | `presentedForm[0]` (text/plain, UTF-8, base64), `conclusion` | all text lines, in order |
| OBX-3 = `&IMP` / `IMP` / LOINC 19005-8 | `conclusion` | the impression; if there is none, the whole text |
| OBX (CE/CWE/CNE) | `conclusionCode[]` | e.g. a Lung-RADS or BI-RADS category |
| OBX (other types: NM, SN…) | not mapped | warning; the raw message is traceable through Provenance |
| ZDS-1 (RP: UID^app^type^subtype) | ImagingStudy `identifier = {system: urn:dicom:uid, value: urn:oid:<UID>}` | the R4 convention for Study Instance UID; must be a valid UID (≤64, digits and dots) |
| OBR-18 | ImagingStudy `identifier` (ACSN) | |
| — | ImagingStudy `status = available`, `basedOn` → ServiceRequest; DiagnosticReport `imagingStudy` → it | only when OBR-25 says the exam was performed (A, P, R, F, C); O, I, S and X create no ImagingStudy |

Report text is read **whole**, per repetition, then unescaped. It is not split into components, because
reporting systems routinely send an unescaped `^` in free text. `\.br\` becomes a line break. `\H\`/`\N\`
(highlighting) and other formatting commands are dropped. `OBX-3 = &IMP` is a real-world convention: the `&` is
the subcomponent separator, so the code actually sits in subcomponent 2.

The ServiceRequest written from an ORU is create-if-absent: `completed` for F/C, `revoked` for X, otherwise
`active`. A result never changes the status of an existing order.

### Z-segments

No FHIR home. Each is kept verbatim on the message's Provenance as an extension
`http://example.org/fhir/StructureDefinition/hl7v2-z-segment` (`valueString`). ZDS-1 is also *read* (Study
Instance UID). OMI's IPC segment is kept the same way, so IPC-3 (the Study Instance UID) is not lost.

## 3. v2 tables → FHIR codes

| v2 table | FHIR element | Map |
|---|---|---|
| 0001 Administrative sex | Patient.gender | M male · F female · O other · U unknown · A other · N unknown |
| 0200 Name type | HumanName.use | L official · D usual · M maiden · N nickname · S anonymous · TEMP, NAV temp · BAD old · A, T and anything else: no use |
| 0190 Address type | Address.use / type | H home · B, O work · C temp · BA old · M home + type postal |
| 0201 Telecom use | ContactPoint.use | PRN, ORN, VHN home · WPN work · PRS mobile · NET → system email · empty → the field's default · anything else: no use |
| 0202 Telecom equipment | ContactPoint.system | PH phone · FX fax · CP phone + use mobile · BP pager · Internet, X.400 email · MD, TDD, TTY, SAT other · empty → phone · anything else other |
| 0203 Identifier type | Identifier.type | same codes in `terminology.hl7.org/CodeSystem/v2-0203` (MR, PI, VN, PLAC, FILL, ACSN…) |
| 0004 Patient class | Encounter.class (v3 ActCode) | E EMER · I IMP · O AMB · P PRENC · R AMB · B IMP · other → NullFlavor UNK |
| 0119 Order control | ServiceRequest.status | NW, OK, XO, XX, RL, OR active (or ORC-5 if present) · SC → ORC-5 required (empty → AE 101; not in table 0038 → AE 103) · CA, CR, OC, DC, DR, OD revoked · HD, OH on-hold · anything else AE 103 |
| 0038 Order status | ServiceRequest.status | A, IP, SC active · CM completed · CA, DC, RP revoked · HD on-hold · ER entered-in-error |
| 0123 Result status | DiagnosticReport.status | O, I, S registered · A, R partial · P preliminary · F final · **C corrected** · X cancelled · anything else (e.g. D, Y, Z) AE 103 |
| 0027 Priority | ServiceRequest.priority | S stat · A asap · R routine · T, P urgent |
| 0074 Diagnostic service section | DR.category (v2-0074); modality | category: 0074 codes as sent (CT, NMR, NMS, RUS, RX…); DICOM modality values (MR, US, NM, MG, PT, XA, RF, DX, CR) are not 0074 codes → `RAD`. Modality: CT→CT · NMR, MR→MR · US→US · NMS, NM→NM · MG, XA, RF, DX, CR as is · PT read as PET (0074 PT is Physical Therapy) |
| 0396 Coding system | Coding.system | LN → loinc.org · SCT (and the non-standard SNOMED, SNOMED-CT) → snomed.info/sct · SNM → terminology.hl7.org/CodeSystem/snm · SNM3 → terminology.hl7.org/CodeSystem/SNM3 · C4/CPT → ama-assn.org/go/cpt · I10 → icd-10 · I10C → icd-10-cm · I9C → icd-9-cm · DCM → DICOM · RADLEX/RID → radlex.org · anything else → `<code_system_base><name>` |
| 0211 Character set | (decoding) | ASCII, 8859/1…/15, UNICODE UTF-8, GB 18030-2000, KS X 1001, BIG-5, plus common non-standard spellings (UTF-8, CP1252…) |
| 0357 Error condition | ACK ERR-3 | 100, 101, 102, 103, 200, 201, 202, 203, 204, 205, 207 |

**Precedence of ORC-1 and ORC-5.** Cancels, discontinues and holds decide on their own. `CA` with a stale ORC-5 of
`SC` is still revoked. For NW/XO/SC/OK/RL/OR, ORC-5 is used when present.

## 4. Identity: what "the same" means

| Resource | Matched on |
|---|---|
| Patient | the MRN: PID-3 repetition of type MR from the first `mrn_authorities` entry that has one; else the first MR; else the first PI or the first identifier at all (with a warning). No PID-3 → AE 101 |
| Encounter | PV1-19 |
| Practitioner | XCN-1 / CNN-1 with its assigning authority (XCN-9 / CNN-9..11); XCN-13 = NPI → us-npi. No authority → one site-wide `<identifier_system_base>provider` system for all senders (warning), so senders must share provider numbering. One id with two different names in one message → the second is a display name only (warning). No id → no resource, display name only (it could never be matched again) |
| ServiceRequest, DiagnosticReport | one ServiceRequest per order number (a second ORC/OBR group with the same number in one message is ignored with a warning); **any** of placer, filler, accession (OR search). If they match two different orders → 412 → AE: that needs a person. If they match one stored order whose placer, filler or accession of the same system has a different value, it is a different order sharing a number → AE 205 (checked by reading the order first; needs the FHIR server). Numbers the server already holds are kept: a message carrying a subset never erases the others, and a late NW adds the numbers it brings. The matched order or report must belong to this message's patient, or to a patient an A40 linked to it; otherwise AE 207 |
| ImagingStudy | Study Instance UID |
| Provenance | deterministic id from sending app + facility + MSH-10 + content hash (MSH-7 ignored) |

**Identifier.system** from the assigning authority (HD): the site map (`[mapping.assigning_authorities]`, keyed by
namespace or universal id) → an ISO OID (`urn:oid:`), UUID (`urn:uuid:`) or URI universal id →
`<identifier_system_base><namespace>` → `<identifier_system_base><universal id>` (with a warning; so does an
invalid OID). An empty PID-3.4 is assumed to be `default_assigning_authority` (with a warning). A patient is never
matched on a bare value, because in FHIR `identifier=1001` matches 1001 in any system: an MRN or MRG-1 with no
authority and no default is AE 101, and a PV1-19 without one writes no Encounter (warning). Order numbers without an authority use
a **site-wide** fallback (`…/placer-order`, `…/filler-order`, `…/accession`). They are deliberately not per
sender, because the ORU comes from a different application than the ORM and must produce the same key.

## 5. Data types

**TS / DTM** `YYYY[MM[DD[HH[MM[SS[.S…]]]]]][+/-ZZZZ]`

| v2 | FHIR date | FHIR dateTime | FHIR instant |
|---|---|---|---|
| `2026` / `202609` / `20260915` | `2026` / `2026-09` / `2026-09-15` | same | omitted + warning |
| `202609151430-0500` | `2026-09-15` | `2026-09-15T14:30:00-05:00` (seconds padded) | same |
| `20260915143015.1234+0530` | `2026-09-15` | `2026-09-15T14:30:15.1234+05:30` | same |
| `20260915143000` (no offset) | `2026-09-15` | offset from `default_timezone`, DST-correct (`-06:00` in January, `-05:00` in July), one warning per message | same |
| no offset and `default_timezone = ""` | `2026-09-15` | `2026-09-15` (truncated to the date, never a made-up offset) | omitted |
| `20260230`, `20261345`, `+2500` | omitted + warning | | |

A FHIR dateTime with a time **must** carry an offset. `fhir.resources` rejects one without, and a test checks
exactly that (`tests/test_validation_ack.py`).

**CX** ID ^ check digit ^ scheme ^ **assigning authority (HD)** ^ **type (0203)** → Identifier `{type, system, value, assigner.display}`.
**EI** entity id ^ namespace ^ universal id ^ universal id type → Identifier with the PLAC/FILL/ACSN type.
**XPN** / **XCN** / **CNN**: see PID-5, and XCN-1 id + XCN-9 authority for Practitioner.identifier.
**XAD**, **XTN**: see PID-11/13/14. **CE/CWE** code ^ text ^ system ^ alt code ^ alt text ^ alt system (^ … ^ original text in CWE-9) → CodeableConcept with up to two codings; `text` from CWE-9, else CE-2, else CE-5.

**Nulls.** An empty field or component means "not sent" and nothing is written. The HL7 explicit null `""` also
reads as empty. For an upsert (ADT) that means the element is **removed** from the resource, which is what
`""` asks for. Values are stripped of surrounding spaces (fixed-width legacy systems pad them).

**Escapes.** `\F\ \S\ \T\ \R\ \E\` → the delimiter; `\Xhh…\` → bytes decoded with the message charset;
`\.br\` and `\.sp n\` → line breaks (n capped at 20; the standard sets no limit). `\.sk n\` → n spaces (capped at 80), `\.ce\` → a line break. `\H\ \N\`, `\.in \.ti \.fi \.nf` and `\Cxxyy\`/`\Mxxyyzz\` (ISO 2022
charset switching) are dropped. Unknown or unterminated sequences are kept literally.

## 6. Deviations and decisions

| Topic | This bridge | Why |
|---|---|---|
| Report OBX | `DiagnosticReport.conclusion` + `presentedForm` (+ `conclusionCode` for coded OBX); **no Observation per OBX** | a radiology report is narrative. One Observation per text line adds resources nobody queries. Numeric OBX are out of scope (warning) |
| OBR-25 = C | `corrected` | the most specific R4 code; `corrected` is a child of `amended` in the R4 hierarchy. One line in `tables.py` to change |
| Message metadata | Provenance (+ `meta.source`), no MessageHeader | this is not FHIR messaging; Provenance says which message wrote which resources and survives in the server |
| Z-segments | kept verbatim on Provenance | the IG does not map them; dropping site data silently is worse |
| Matching / identity | conditional requests on identifiers, OR across order numbers, MRN choice by configured authority | the IG leaves identity to implementers; this is the part that decides whether replays duplicate |
| Practitioner without an id | display-only reference | a name-only Practitioner would duplicate on every message |
| Encounter without PV1-19 | not written | same reason |
| Unmappable Encounter class | `v3-NullFlavor#UNK` | `Encounter.class` is 1..1 in R4 |
| Modality on an order | `ServiceRequest.orderDetail` (DICOM coding) | R4 ServiceRequest has no modality element |
| A40 | upsert both records, linked | FHIR R4 has no standard merge operation. If the server offers one, use it instead |
| Older report after a newer one | the DiagnosticReport entry is dropped (warning) after reading the current report: status rank registered < partial < preliminary, cancelled < final < amended, corrected, appended < entered-in-error; at equal rank the older `issued` (OBR-22) loses | a conditional PUT cannot say "only if newer"; table 0123 F "can only be changed with a corrected result"; needs the FHIR server, not the directory sink |
| Status change on a finished order | completed may still become revoked or entered-in-error, revoked only entered-in-error; anything else is dropped (warning) after reading the current order | R4 request-status: completed and revoked mean no further activity; stops a resent SC or RL re-opening a cancelled order |
| Status-only order messages | FHIRPath Patch of `status` | a full PUT from an ORC-only cancel would erase the procedure and requester |
| Unsupported events (A03, SIU…) | AA + warning by default | a NAK blocks the sender's queue for a message nobody needs; `reject` is one setting away |
