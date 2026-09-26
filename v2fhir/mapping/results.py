"""ORU^R01 radiology result -> DiagnosticReport (+ ServiceRequest, ImagingStudy, Practitioner).

Per OBR group:
  ServiceRequest    create-if-absent on the same key chain as the order (placer -> filler -> accession).
                    If the ORU arrives before its ORM, this creates the order from the OBR; when the ORM
                    finally arrives its NW matches and changes nothing. A result never changes an existing
                    order's status: the RIS owns order status (ORM SC/CA), the reporting system owns the report.
  ImagingStudy      create-if-absent on the Study Instance UID, when ZDS-1 carries one (IHE convention).
  DiagnosticReport  upsert on the order key: preliminary -> final -> corrected are versions of ONE report.
                    status from OBR-25 (table 0123), basedOn the ServiceRequest, imagingStudy, conclusion
                    from the impression OBX lines, the whole report text as presentedForm.

Report text: TX/FT/ST OBX values are read *whole* and unescaped, not split into components: reporting
systems routinely send an unescaped "^" in free text ("T2^weighted"), which a component split would cut.
"""
from __future__ import annotations

import base64
import copy
import re

from ..errors import REQUIRED_FIELD_MISSING, TABLE_VALUE_NOT_FOUND, MappingError
from ..hl7.parser import Message, Segment
from . import datatypes as dt
from . import tables as T
from .context import Ctx
from .orders import order_groups, order_identifiers, service_request
from .patient import add_encounter, add_patient, practitioner_ref

UID_RX = re.compile(r"^[0-2](\.(0|[1-9][0-9]*))+$")
FHIR_STRING_MAX_BYTES = 1024 * 1024          # a FHIR string is at most 1 MB; base64Binary (presentedForm) has no limit
TRUNCATED = " [truncated; full report in presentedForm]"


def _fit_string(s: str, limit: int = FHIR_STRING_MAX_BYTES) -> tuple[str, bool]:
    raw = s.encode("utf-8")
    if len(raw) <= limit:
        return s, False
    cut = limit - len(TRUNCATED.encode("utf-8"))
    return raw[:cut].decode("utf-8", errors="ignore") + TRUNCATED, True


def _obx_text(obx: Segment, msg: Message) -> str:
    return dt.ft_whole(obx, 5, msg)


def _obx_code(obx: Segment, msg: Message) -> str:
    # OBX-3 "&IMP" / "&GDT": the "&" is the subcomponent separator, so the code sits in subcomponent 2.
    return msg.unescape(obx.rep(3).component_raw(1)).replace(msg.delimiters.subcomponent or "&", "").strip().upper()


def report_text(obxs: list[Segment], ctx: Ctx) -> tuple[str, str, list[dict]]:
    """Returns (full text, impression, coded conclusions)."""
    body: list[str] = []
    impression: list[str] = []
    coded: list[dict] = []
    for obx in obxs:
        vt = (obx.get(2) or "").upper()
        if vt in T.TEXT_VALUE_TYPES:
            text = _obx_text(obx, ctx.msg)
            body.append(text)
            if _obx_code(obx, ctx.msg) in T.IMPRESSION_CODES:
                impression.append(text)
        elif vt in T.CODED_VALUE_TYPES:
            cc = dt.ce(obx.rep(5), ctx)
            if cc:
                coded.append(cc)
        elif obx.raw(5):
            ctx.warn(f"OBX value type {vt or '(empty)'!r} is not mapped (kept in provenance only)", obx.loc(2))
    return "\n".join(body).strip("\n"), "\n".join(impression).strip("\n"), coded


def convert_result_message(msg: Message, ctx: Ctx) -> None:
    groups = [g for g in order_groups(msg) if "OBR" in g]
    if not groups:
        raise MappingError("ORU has no OBR segment", REQUIRED_FIELD_MISSING)
    _, patient_ref = add_patient(msg.seg("PID"), ctx, authoritative=False)
    encounter = add_encounter(msg.seg("PV1"), patient_ref, ctx, authoritative=False)
    for g in groups:
        obr = g["OBR"][0]
        orc = g.get("ORC", [None])[0]
        idents, key = order_identifiers(orc, obr, None, ctx)
        if not key:
            raise MappingError("result has no placer (OBR-2), filler (OBR-3) or accession (OBR-18) number", REQUIRED_FIELD_MISSING, obr.loc(2))
        rs = (obr.get(25) or "").upper()
        if not rs:
            raise MappingError("OBR-25 (result status) is empty", REQUIRED_FIELD_MISSING, obr.loc(25))
        if rs not in T.RESULT_STATUS_0123:
            raise MappingError(f"OBR-25 result status {rs!r} is not in table 0123", TABLE_VALUE_NOT_FOUND, obr.loc(25))
        code = dt.ce(obr.rep(4), ctx)
        if not code:
            raise MappingError("OBR-4 (universal service id) is empty; DiagnosticReport.code is required", REQUIRED_FIELD_MISSING, obr.loc(4))

        sr = service_request(ctx, status=T.RESULT_STATUS_TO_ORDER.get(rs, "active"), idents=copy.deepcopy(idents), orc=orc, obr=obr,
                             tq1=None, ipc=None, dg1s=[], ntes=[], subject=patient_ref, encounter=encounter)
        sr_url = ctx.tx.create_if_absent(sr, key, also=idents)

        performed = rs in T.RESULT_STATUS_EXAM_PERFORMED        # O, I, S, X: no images exist yet (or ever)
        study_ref = _imaging_study(g, obr, idents, patient_ref, encounter, sr_url, ctx) if performed else None

        text, impression, coded = report_text(g.get("OBX", []), ctx)
        dr: dict = {"resourceType": "DiagnosticReport", "meta": ctx.meta(), "identifier": copy.deepcopy(idents),
                    "basedOn": [{"reference": sr_url}], "status": T.RESULT_STATUS_0123[rs]}
        dr["category"] = [_category(obr, ctx)]
        dr["code"] = code
        dr["subject"] = patient_ref
        if encounter:
            dr["encounter"] = encounter
        eff = dt.ts(obr.get(7), "dateTime", ctx, obr.loc(7))
        if eff:
            dr["effectiveDateTime"] = eff
        issued = dt.ts(obr.get(22), "instant", ctx, obr.loc(22))
        if issued:
            dr["issued"] = issued
        interp = [r for r in (practitioner_ref(*dt.cnn_in_ndl(rep, ctx), ctx) for rep in obr.reps(32)) if r]
        if interp:
            dr["resultsInterpreter"] = interp
        if study_ref:
            dr["imagingStudy"] = [study_ref]
        if impression or text:
            dr["conclusion"], cut = _fit_string(impression or text)
            if cut:
                ctx.warn("report text is over FHIR's 1 MB string limit; conclusion truncated, full text in presentedForm", obr.loc())
        if coded:
            dr["conclusionCode"] = coded
        if text:
            dr["presentedForm"] = [{"contentType": "text/plain; charset=utf-8", "language": "en",
                                    "data": base64.b64encode(text.encode("utf-8")).decode("ascii"), "title": "Radiology report"}]
        if rs != "X" and not text and not coded:
            ctx.warn("result has no report text (no TX/FT/ST OBX)", obr.loc())
        if ctx.tx.has("DiagnosticReport", key):
            ctx.warn("a second OBR for the same order in one message; only the first report is used", obr.loc())
            continue
        ctx.tx.upsert(dr, key, also=idents)


def _category(obr: Segment, ctx: Ctx) -> dict:
    """OBR-24 -> DiagnosticReport.category (v2-0074). Only real table 0074 codes go into the coding; anything
    else (a DICOM modality such as MR, or a local code) becomes RAD with the sent value kept as text."""
    sent = obr.get(24) or ""
    section = sent.upper()
    code = section if section in T.SERVICE_SECTION_0074 and section not in T.DICOM_NOT_0074 else "RAD"
    category: dict = {"coding": [{"system": T.V2 + "0074", "code": code}]}
    if section and code != section:
        category["text"] = sent
        if section not in T.DICOM_NOT_0074:
            ctx.warn(f"OBR-24 {sent!r} is not in table 0074; category RAD", obr.loc(24))
    return category


def _imaging_study(g, obr: Segment, idents: list[dict], patient_ref: dict, encounter: dict | None, sr_url: str, ctx: Ctx) -> dict | None:
    zds = g.get("ZDS", [None])[0]
    uid = zds.get(1) if zds is not None else None
    if not uid:
        return None
    if len(uid) > 64 or not UID_RX.match(uid):
        ctx.warn(f"ZDS-1 {uid[:70]!r} is not a valid DICOM UID; no ImagingStudy", zds.loc(1))
        return None
    uid_ident = {"system": T.DICOM_UID_SYSTEM, "value": f"urn:oid:{uid}"}
    study: dict = {"resourceType": "ImagingStudy", "meta": ctx.meta(), "identifier": [uid_ident]}
    study["identifier"] += [copy.deepcopy(i) for i in idents if (i.get("type") or {}).get("coding", [{}])[0].get("code") == "ACSN"]
    study["status"] = "available"
    mod = dt.modality_coding(obr.get(24))
    if mod:
        study["modality"] = [mod]
    study["subject"] = patient_ref
    if encounter:
        study["encounter"] = encounter
    study["basedOn"] = [{"reference": sr_url}]
    return {"reference": ctx.tx.create_if_absent(study, uid_ident)}
