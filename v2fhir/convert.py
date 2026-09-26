"""Parsed v2 message -> FHIR R4 transaction Bundle. Pure: no I/O, no server state."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .errors import REQUIRED_FIELD_MISSING, UNSUPPORTED_EVENT_CODE, UNSUPPORTED_MESSAGE_TYPE, Issue, MappingError, RejectError
from .hl7.parser import Message
from .mapping.context import Ctx
from .mapping.orders import convert_order_message
from .mapping.patient import add_encounter, add_merge, add_patient
from .mapping.provenance import add_provenance
from .mapping.results import convert_result_message

SUPPORTED = {
    ("ADT", "A01"): "admit", ("ADT", "A04"): "register", ("ADT", "A08"): "update patient", ("ADT", "A40"): "merge patient",
    ("ORM", "O01"): "order", ("OMI", "O23"): "imaging order", ("ORU", "R01"): "result",
}


class UnsupportedMessage(RejectError):
    pass


@dataclass
class Conversion:
    message: Message
    bundle: dict
    warnings: list[Issue]


def check_supported(msg: Message) -> None:
    mt, ev = msg.message_type.upper(), msg.trigger.upper()
    if (mt, ev) in SUPPORTED:
        return
    code = UNSUPPORTED_EVENT_CODE if mt in {m for m, _ in SUPPORTED} else UNSUPPORTED_MESSAGE_TYPE
    raise UnsupportedMessage(f"{mt}^{ev} is not handled by this bridge", code, msg.msh.loc(9))


def convert(msg: Message, cfg: Config, now: datetime | None = None) -> Conversion:
    check_supported(msg)
    ctx = Ctx(msg=msg, cfg=cfg, now=now or datetime.now(timezone.utc))
    mt, ev = msg.message_type.upper(), msg.trigger.upper()
    if mt == "ADT" and ev == "A40":
        _merge(msg, ctx)
    elif mt == "ADT":
        _, patient_ref = add_patient(msg.seg("PID"), ctx, authoritative=True)
        add_encounter(msg.seg("PV1"), patient_ref, ctx, authoritative=True)
    elif mt in ("ORM", "OMI"):
        convert_order_message(msg, ctx)
    else:
        convert_result_message(msg, ctx)
    add_provenance(msg, ctx)
    ctx.finish()
    bundle = ctx.tx.bundle(identifier={"system": ctx.message_system, "value": msg.control_id}, timestamp=ctx.now.isoformat(timespec="seconds"))
    warnings = [Issue("0", w, None, "W") for w in msg.warnings] + ctx.warnings
    return Conversion(msg, bundle, warnings)


def _merge(msg: Message, ctx: Ctx) -> None:
    pairs = []
    pid = None
    for seg in msg.segments:
        if seg.name == "PID":
            pid = seg
        elif seg.name == "MRG":
            if pid is None:
                raise MappingError("MRG without a preceding PID", REQUIRED_FIELD_MISSING, seg.loc())
            pairs.append((pid, seg))
    if not pairs:
        raise MappingError("A40 has no MRG segment", REQUIRED_FIELD_MISSING)
    for pid, mrg in pairs:
        add_merge(pid, mrg, ctx)
