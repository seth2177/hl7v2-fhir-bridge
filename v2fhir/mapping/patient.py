"""PID -> Patient, PV1 -> Encounter, XCN -> Practitioner, MRG -> Patient.link (merge)."""
from __future__ import annotations

from ..errors import REQUIRED_FIELD_MISSING, MappingError
from ..hl7.parser import Segment
from . import datatypes as dt
from . import tables as T
from .context import Ctx


# ---- identifiers and the MRN -------------------------------------------------------------------
def identifiers(seg: Segment, field: int, ctx: Ctx) -> list[tuple[dict, str | None]]:
    """All CX repetitions of a field as (Identifier, assigning-authority namespace)."""
    out = []
    for rep in seg.reps(field):
        ident = dt.cx(rep, ctx, seg.loc(field), ctx.cfg.default_assigning_authority or None)
        if ident:
            out.append((ident, rep.get(4, 1) or (ctx.cfg.default_assigning_authority if not rep.get(4, 2) else None)))
    return out


def choose_mrn(ids: list[tuple[dict, str | None]], ctx: Ctx, where) -> dict:
    """Pick the one identifier the patient is matched on.

    PID-3 often repeats (the hospital MRN, an enterprise id, a clinic's number). Matching on the wrong
    one merges or splits patients, so the choice is explicit:
      1. type MR from a configured `mrn_authorities` entry (in configured order)
      2. otherwise the first type MR
      3. otherwise the first PI, then the first identifier at all -- with a warning
    """
    if not ids:
        raise MappingError("no patient identifier (PID-3 is empty)", REQUIRED_FIELD_MISSING, where)

    def type_of(ident):
        return ((ident.get("type") or {}).get("coding") or [{}])[0].get("code")

    for auth in ctx.cfg.mrn_authorities:
        for ident, ns in ids:
            if ns == auth and type_of(ident) in ("MR", None):
                return ident
    mrs = [i for i, _ in ids if type_of(i) == "MR"]
    if mrs:
        if len(mrs) > 1:
            ctx.warn(f"{len(mrs)} MR identifiers and none from a configured MRN authority; matched on the first", where)
        return mrs[0]
    pis = [i for i, _ in ids if type_of(i) == "PI"]
    chosen = pis[0] if pis else ids[0][0]
    ctx.warn(f"no MR-typed identifier; matched patient on {chosen.get('system')}|{chosen['value']}", where)
    return chosen


# ---- Patient ------------------------------------------------------------------------------------
def patient_resource(pid: Segment, ctx: Ctx) -> tuple[dict, dict]:
    ids = identifiers(pid, 3, ctx)
    mrn = choose_mrn(ids, ctx, pid.loc(3))
    for ident, _ in ids:
        if ident is mrn:
            ident["use"] = "usual"
    p: dict = {"resourceType": "Patient", "meta": ctx.meta(), "identifier": [i for i, _ in ids], "active": True}
    names = [n for n in (dt.xpn(r) for r in pid.reps(5)) if n]
    if names:
        p["name"] = names
    telecom = [t for t in (dt.xtn(r, "home") for r in pid.reps(13)) if t] + [t for t in (dt.xtn(r, "work") for r in pid.reps(14)) if t]
    if telecom:
        p["telecom"] = telecom
    sex = pid.get(8)
    if sex:
        if sex.upper() in T.SEX_0001:
            p["gender"] = T.SEX_0001[sex.upper()]
        else:
            ctx.warn(f"PID-8 sex {sex!r} is not in table 0001; gender left empty", pid.loc(8))
    birth = dt.ts(pid.get(7), "date", ctx, pid.loc(7))
    if birth:
        p["birthDate"] = birth
    addresses = [a for a in (dt.xad(r) for r in pid.reps(11)) if a]
    if addresses:
        p["address"] = addresses
    death_ts = dt.ts(pid.get(29), "dateTime", ctx, pid.loc(29))
    if death_ts:
        p["deceasedDateTime"] = death_ts
    elif (pid.get(30) or "").upper() == "Y":
        p["deceasedBoolean"] = True
    elif (pid.get(30) or "").upper() == "N":
        p["deceasedBoolean"] = False
    return p, mrn


def add_patient(pid: Segment | None, ctx: Ctx, *, authoritative: bool) -> tuple[str, dict]:
    """ADT is the source of truth for demographics (upsert). Orders and results only make sure the
    patient exists (create-if-absent), so a stale PID on an old order can't overwrite a newer A08."""
    if pid is None:
        raise MappingError("message has no PID segment", REQUIRED_FIELD_MISSING)
    resource, mrn = patient_resource(pid, ctx)
    full_url = ctx.tx.upsert(resource, mrn) if authoritative else ctx.tx.create_if_absent(resource, mrn)
    return full_url, _ref(full_url, resource.get("name"))


def _ref(full_url: str, names: list | None) -> dict:
    ref = {"reference": full_url}
    text = dt.name_text(names[0]) if names else ""
    if text:
        ref["display"] = text
    return ref


# ---- Practitioner -------------------------------------------------------------------------------
def practitioner_ref(ident: dict | None, name: dict | None, ctx: Ctx) -> dict | None:
    """A provider with an id becomes a Practitioner (create-if-absent). One without an id is only a
    display name on the reference: creating a Practitioner we could never match again would make a
    new duplicate on every message."""
    if not ident and not name:
        return None
    if not ident:
        return {"display": dt.name_text(name)}
    res: dict = {"resourceType": "Practitioner", "meta": ctx.meta(), "identifier": [ident]}
    if name:
        res["name"] = [name]
    full_url = ctx.tx.create_if_absent(res, ident)
    ref = {"reference": full_url}
    if name:
        ref["display"] = dt.name_text(name)
    return ref


def xcn_ref(seg: Segment, field: int, ctx: Ctx) -> dict | None:
    reps = seg.reps(field)
    if not reps:
        return None
    ident, name = dt.xcn(reps[0], ctx)
    return practitioner_ref(ident, name, ctx)


# ---- Encounter ----------------------------------------------------------------------------------
def add_encounter(pv1: Segment | None, patient_ref: dict, ctx: Ctx, *, authoritative: bool) -> dict | None:
    """PV1 -> Encounter, keyed on the visit number (PV1-19). Without a visit number there is nothing
    stable to match on, so no Encounter is written (a warning says so)."""
    if pv1 is None:
        return None
    visit = pv1.reps(19)
    ident = dt.cx(visit[0], ctx, pv1.loc(19), ctx.cfg.default_assigning_authority or None) if visit else None
    if not ident:
        if any(pv1.raw(n) for n in (2, 3, 7, 44)):
            ctx.warn("PV1-19 (visit number) is empty; no Encounter written", pv1.loc(19))
        return None
    if "type" not in ident:
        ident["type"] = dt.identifier_type("VN")
    enc: dict = {"resourceType": "Encounter", "meta": ctx.meta(), "identifier": [ident]}
    start = dt.ts(pv1.get(44), "dateTime", ctx, pv1.loc(44))
    end = dt.ts(pv1.get(45), "dateTime", ctx, pv1.loc(45))
    enc["status"] = "finished" if end else "in-progress"
    klass = pv1.get(2)
    if klass and klass.upper() in T.PATIENT_CLASS_0004:
        code, display = T.PATIENT_CLASS_0004[klass.upper()]
        enc["class"] = {"system": T.V3_ACTCODE, "code": code, "display": display}
    else:
        if klass:
            ctx.warn(f"PV1-2 patient class {klass!r} is not mapped", pv1.loc(2))
        enc["class"] = {"system": T.V3_NULLFLAVOR, "code": "UNK", "display": "unknown"}
    enc["subject"] = patient_ref
    participants = []
    for field, code, display in ((7, "ATND", "attender"), (8, "REF", "referrer"), (17, "ADM", "admitter")):
        ref = xcn_ref(pv1, field, ctx)
        if ref:
            participants.append({"type": [{"coding": [{"system": T.V3_PARTICIPATION, "code": code, "display": display}]}], "individual": ref})
    if participants:
        enc["participant"] = participants
    if start or end:
        enc["period"] = {k: v for k, v in (("start", start), ("end", end)) if v}
    loc = pv1.rep(3)
    loc_text = "^".join(x for x in (loc.get(1), loc.get(2), loc.get(3), loc.get(4)) if x)
    if loc_text:
        enc["location"] = [{"location": {"display": loc_text}}]
    full_url = ctx.tx.upsert(enc, ident) if authoritative else ctx.tx.create_if_absent(enc, ident)
    return {"reference": full_url}


# ---- merge (ADT^A40) ----------------------------------------------------------------------------
def add_merge(pid: Segment, mrg: Segment, ctx: Ctx) -> None:
    """A40: the patient in MRG-1 is merged into the patient in PID-3.

    Surviving Patient (from PID, upsert) gets link type "replaces"; the retired one (from MRG, upsert)
    is set active=false with link type "replaced-by". The retired record keeps its identifiers so old
    references and searches still resolve to it, and from there to the survivor.
    """
    survivor, s_mrn = patient_resource(pid, ctx)
    old_ids = identifiers(mrg, 1, ctx)
    old_mrn = choose_mrn(old_ids, ctx, mrg.loc(1))
    if (old_mrn.get("system"), old_mrn["value"]) == (s_mrn.get("system"), s_mrn["value"]):
        raise MappingError("MRG-1 names the same patient as PID-3; nothing to merge", "205", mrg.loc(1))
    old: dict = {"resourceType": "Patient", "meta": ctx.meta(), "identifier": [i for i, _ in old_ids], "active": False}
    old_name = [n for n in (dt.xpn(r) for r in mrg.reps(7)) if n] or survivor.get("name")
    if old_name:
        old["name"] = old_name
    s_url, o_url = ctx.tx.url_for("Patient", s_mrn), ctx.tx.url_for("Patient", old_mrn)
    survivor["link"] = [{"other": {"reference": o_url}, "type": "replaces"}]
    old["link"] = [{"other": {"reference": s_url}, "type": "replaced-by"}]
    ctx.tx.upsert(survivor, s_mrn)
    ctx.tx.upsert(old, old_mrn)
