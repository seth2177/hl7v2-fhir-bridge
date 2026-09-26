"""One Provenance per message: which v2 message (MSH-10) wrote which resources, and when.

R4 Provenance has no identifier to search on, so it is written with PUT to an id derived from
sending application + facility + MSH-10: replaying the same message rewrites the same Provenance
instead of adding another. Every resource also carries meta.source = urn:hl7v2:<app>:<facility>#<MSH-10>.

Z-segments have no FHIR home, so each one is kept verbatim on the Provenance (extension below):
site-specific data is never silently thrown away, and it stays next to the resources it came with.
"""
from __future__ import annotations

from ..hl7.parser import Message
from . import datatypes as dt
from . import tables as T
from .context import Ctx

Z_SEGMENT_EXTENSION = "http://example.org/fhir/StructureDefinition/hl7v2-z-segment"


def add_provenance(msg: Message, ctx: Ctx) -> None:
    targets = ctx.tx.target_refs()
    if not targets:
        return
    prov: dict = {"resourceType": "Provenance", "id": ctx.provenance_id, "meta": ctx.meta()}
    z = [str(s) for s in msg.z_segments]
    if z:
        prov["extension"] = [{"url": Z_SEGMENT_EXTENSION, "valueString": text} for text in z]
    prov["target"] = targets
    occurred = dt.ts(msg.msh.get(7), "dateTime", ctx, msg.msh.loc(7))
    if occurred:
        prov["occurredDateTime"] = occurred
    prov["recorded"] = ctx.now.isoformat(timespec="seconds")
    prov["activity"] = {"coding": [{"system": T.V2 + "0003", "code": msg.trigger}], "text": msg.type_label}
    who = msg.sending_app + (f" ({msg.sending_facility})" if msg.sending_facility else "")
    prov["agent"] = [
        {"type": {"coding": [{"system": T.PROVENANCE_PARTICIPANT, "code": "author", "display": "Author"}]}, "who": {"display": who or "unknown sender"}},
        {"type": {"coding": [{"system": T.PROVENANCE_PARTICIPANT, "code": "assembler", "display": "Assembler"}]}, "who": {"display": "hl7v2-fhir-bridge"}},
    ]
    prov["entity"] = [{"role": "source", "what": {"identifier": {"system": ctx.message_system, "value": msg.control_id},
                                                  "display": f"HL7 v2 {msg.type_label} control id {msg.control_id}"}}]
    ctx.tx.put_by_id(prov)
