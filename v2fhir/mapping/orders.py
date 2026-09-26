"""ORM^O01 / OMI^O23 imaging orders -> ServiceRequest.

One ORC starts an order group; the OBR, TQ1, IPC, ZDS, NTE and DG1 after it belong to that order.

Which order is it? (the conditional match)
  Every order number the message carries -- placer (ORC-2, else OBR-2), filler (ORC-3, else OBR-3),
  accession -- goes into the search as an OR: identifier=placer,filler,accession. Systems rarely send the
  same subset (the RIS knows all three, a reporting system may only echo the filler number), and a match
  on any of them finds the order. If they point at two different orders the server answers 412 and the
  message is NAKed: that needs a human, not a guess. The bundle-internal key is placer -> filler -> accession.

Accession number: IHE Scheduled Workflow carries it in OBR-18 for ORM^O01 and in IPC-1 for OMI^O23.
The Study Instance UID is ZDS-1 (ORM) or IPC-3 (OMI); it is kept in provenance with the other Z data
and becomes an ImagingStudy when a result arrives.

How it is written (see mapping/bundle.py):
  NW                             create-if-absent. A replayed NW, or an NW arriving after its result
                                 (ORU before ORM), matches the existing order and changes nothing.
  any other control, full order  upsert: the message carries the whole order (PID + OBR).
  any other control, ORC only    patch ServiceRequest.status. A status-only message cannot rebuild the
                                 resource, and replacing it would wipe the procedure and requester.
                                 Unknown order -> the server answers 404 -> AE 204 "unknown key".
"""
from __future__ import annotations

from ..errors import REQUIRED_FIELD_MISSING, TABLE_VALUE_NOT_FOUND, MappingError
from ..hl7.parser import Message, Segment
from . import datatypes as dt
from . import tables as T
from .bundle import replace_op
from .context import Ctx
from .patient import add_encounter, add_patient, xcn_ref


def order_groups(msg: Message) -> list[dict[str, list[Segment]]]:
    """Split a message into order groups: ORC starts one; an OBR joins the ORC before it, or starts a
    group of its own when that ORC already has one (ORU may omit ORC). Segments before the first
    group (PID, PV1, patient NTEs) are not part of any order."""
    groups: list[dict[str, list[Segment]]] = []
    cur: dict[str, list[Segment]] | None = None
    for seg in msg.segments:
        if seg.name == "ORC":
            cur = {"ORC": [seg]}
            groups.append(cur)
        elif seg.name == "OBR":
            if cur is None or "OBR" in cur:
                cur = {"OBR": [seg]}
                groups.append(cur)
            else:
                cur["OBR"] = [seg]
        elif cur is not None and seg.name in ("TQ1", "TQ2", "IPC", "ZDS", "NTE", "DG1", "OBX"):
            cur.setdefault(seg.name, []).append(seg)
    return groups


def order_identifiers(orc: Segment | None, obr: Segment | None, ipc: Segment | None, ctx: Ctx) -> tuple[list[dict], dict | None]:
    """Returns (identifiers, the one used as the key)."""
    def first(field: int) -> tuple[Segment | None, int]:
        for s in (orc, obr):
            if s is not None and s.get(field):
                return s, field
        return None, field

    out: list[dict] = []
    s, f = first(2)
    placer = dt.ei_rep(s.rep(f), "PLAC", "placer-order", ctx) if s else None
    s, f = first(3)
    filler = dt.ei_rep(s.rep(f), "FILL", "filler-order", ctx) if s else None
    acc = None
    if ipc is not None and ipc.get(1):
        acc = dt.ei_rep(ipc.rep(1), "ACSN", "accession", ctx)
    elif obr is not None and obr.get(18):
        acc = dt.ei(obr.get(18), None, None, None, "ACSN", "accession", ctx)
    for i in (placer, filler, acc):
        if i:
            out.append(i)
    return out, (placer or filler or acc)


def order_code(obr: Segment | None, ctx: Ctx) -> dict | None:
    return dt.ce(obr.rep(4), ctx) if obr is not None else None


def modality(obr: Segment | None, ipc: Segment | None) -> dict | None:
    if ipc is not None and ipc.get(5):
        return dt.modality_coding(ipc.get(5))
    if obr is not None:
        return dt.modality_coding(obr.get(24))
    return None


def status_from_orc(orc: Segment, ctx: Ctx) -> str:
    control = (orc.get(1) or "").upper()
    if control not in T.ORDER_CONTROL_0119:
        raise MappingError(f"ORC-1 order control {control or '(empty)'!r} is not supported", TABLE_VALUE_NOT_FOUND, orc.loc(1))
    order_status = (orc.get(5) or "").upper()
    if control in T.ORDER_CONTROL_DEFERS_TO_ORC5 and order_status:
        if order_status in T.ORDER_STATUS_0038:
            return T.ORDER_STATUS_0038[order_status]
        if T.ORDER_CONTROL_0119[control] is None:
            raise MappingError(f"ORC-1 is {control} but ORC-5 order status {order_status!r} is not in table 0038", TABLE_VALUE_NOT_FOUND, orc.loc(5))
        ctx.warn(f"ORC-5 order status {order_status!r} is not mapped; using ORC-1", orc.loc(5))
    status = T.ORDER_CONTROL_0119[control]
    if status is None:
        raise MappingError("ORC-1 is SC (status changed) but ORC-5 (order status) is empty", REQUIRED_FIELD_MISSING, orc.loc(5))
    return status


def priority(orc: Segment | None, obr: Segment | None, tq1: Segment | None) -> str | None:
    for code in ((tq1.get(9) if tq1 is not None else None), (obr.get(27, 6) if obr is not None else None),
                 (orc.get(7, 6) if orc is not None else None)):
        if code and code.upper() in T.PRIORITY_0027:
            return T.PRIORITY_0027[code.upper()]
    return None


def service_request(ctx: Ctx, *, status: str, idents: list[dict], orc: Segment | None, obr: Segment | None, tq1: Segment | None,
                    ipc: Segment | None, dg1s: list[Segment], ntes: list[Segment], subject: dict, encounter: dict | None) -> dict:
    sr: dict = {"resourceType": "ServiceRequest", "meta": ctx.meta(), "identifier": idents, "status": status, "intent": "order",
                "category": [T.IMAGING_CATEGORY]}
    code = order_code(obr, ctx)
    if code:
        sr["code"] = code
    mod = modality(obr, ipc)
    if mod:
        sr["orderDetail"] = [{"coding": [mod], "text": f"Modality {mod['code']}"}]
    prio = priority(orc, obr, tq1)
    if prio:
        sr["priority"] = prio
    sr["subject"] = subject
    if encounter:
        sr["encounter"] = encounter
    when = None
    if tq1 is not None:
        when = dt.ts(tq1.get(7), "dateTime", ctx, tq1.loc(7))
    if not when and obr is not None:
        when = dt.ts(obr.get(36), "dateTime", ctx, obr.loc(36)) or dt.ts(obr.get(27, 4), "dateTime", ctx, obr.loc(27, 4))
    if when:
        sr["occurrenceDateTime"] = when
    if orc is not None:
        authored = dt.ts(orc.get(9), "dateTime", ctx, orc.loc(9))
        if authored:
            sr["authoredOn"] = authored
    requester = (xcn_ref(orc, 12, ctx) if orc is not None else None) or (xcn_ref(obr, 16, ctx) if obr is not None else None)
    if requester:
        sr["requester"] = requester
    reasons = [c for c in (dt.ce(r, ctx) for r in (obr.reps(31) if obr is not None else [])) if c]
    reasons += [c for c in (dt.ce(d.rep(3), ctx) for d in dg1s) if c]
    if reasons:
        sr["reasonCode"] = reasons
    notes = [n.get(3) for n in ntes if n.get(3)]
    if notes:
        sr["note"] = [{"text": t} for t in notes]
    return sr


def convert_order_message(msg: Message, ctx: Ctx) -> None:
    pid = msg.seg("PID")
    groups = [g for g in order_groups(msg) if "ORC" in g]
    if not groups:
        raise MappingError("order message has no ORC segment", REQUIRED_FIELD_MISSING)
    patient_ref = None
    encounter = None
    for g in groups:
        orc = g["ORC"][0]
        obr = g.get("OBR", [None])[0]
        tq1 = g.get("TQ1", [None])[0]
        ipc = g.get("IPC", [None])[0]
        idents, key = order_identifiers(orc, obr, ipc, ctx)
        if not key:
            raise MappingError("order has no placer (ORC-2), filler (ORC-3) or accession number", REQUIRED_FIELD_MISSING, orc.loc(2))
        status = status_from_orc(orc, ctx)
        control = (orc.get(1) or "").upper()
        if obr is None or pid is None:
            if control in T.ORDER_CONTROL_IS_NEW:
                raise MappingError("new order (NW) without " + ("OBR" if obr is None else "PID"), REQUIRED_FIELD_MISSING, orc.loc(1))
            ctx.tx.patch("ServiceRequest", key, [replace_op("ServiceRequest.status", "valueCode", status)], also=idents)
            continue
        if patient_ref is None:
            _, patient_ref = add_patient(pid, ctx, authoritative=False)
            encounter = add_encounter(msg.seg("PV1"), patient_ref, ctx, authoritative=False)
        sr = service_request(ctx, status=status, idents=idents, orc=orc, obr=obr, tq1=tq1, ipc=ipc, dg1s=g.get("DG1", []),
                             ntes=g.get("NTE", []), subject=patient_ref, encounter=encounter)
        if control in T.ORDER_CONTROL_IS_NEW:
            ctx.tx.create_if_absent(sr, key, also=idents)
        else:
            ctx.tx.upsert(sr, key, also=idents)
