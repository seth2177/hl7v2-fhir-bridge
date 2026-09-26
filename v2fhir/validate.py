"""Validate generated bundles before they leave the bridge.

Two layers:
  1. fhir.resources (pydantic models generated from the FHIR spec): structure, cardinality, data types,
     primitive formats (a dateTime with a time but no offset is rejected here). The R4B models are used;
     R4B changed nothing in the resources this bridge writes (Patient, Encounter, Practitioner,
     ServiceRequest, DiagnosticReport, ImagingStudy, Provenance, Parameters, Bundle).
  2. Required value-set bindings, which fhir.resources does not check: `gender: "bogus"` passes the
     models, so the codes this bridge emits for required bindings are checked here against the spec's lists.
"""
from __future__ import annotations

from fhir.resources.R4B.bundle import Bundle
from pydantic import ValidationError

REQUIRED_BINDINGS: dict[str, dict[str, set[str]]] = {
    "Patient": {"gender": {"male", "female", "other", "unknown"}},
    "Encounter": {"status": {"planned", "arrived", "triaged", "in-progress", "onleave", "finished", "cancelled", "entered-in-error", "unknown"}},
    "ServiceRequest": {"status": {"draft", "active", "on-hold", "revoked", "completed", "entered-in-error", "unknown"},
                       "intent": {"proposal", "plan", "directive", "order", "original-order", "reflex-order", "filler-order", "instance-order", "option"},
                       "priority": {"routine", "urgent", "asap", "stat"}},
    "DiagnosticReport": {"status": {"registered", "partial", "preliminary", "final", "amended", "corrected", "appended", "cancelled",
                                    "entered-in-error", "unknown"}},
    "ImagingStudy": {"status": {"registered", "available", "cancelled", "entered-in-error", "unknown"}},
}
NESTED_BINDINGS = {
    "use@name": {"usual", "official", "temp", "nickname", "anonymous", "old", "maiden"},
    "use@telecom": {"home", "work", "temp", "old", "mobile"},
    "system@telecom": {"phone", "fax", "email", "pager", "url", "sms", "other"},
    "use@address": {"home", "work", "temp", "old", "billing"},
    "type@address": {"postal", "physical", "both"},
    "use@identifier": {"usual", "official", "temp", "secondary", "old"},
    "type@link": {"replaced-by", "replaces", "refer", "seealso"},
}
HTTP_VERBS = {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"}
SERVICE_REQUEST_STATUS = REQUIRED_BINDINGS["ServiceRequest"]["status"]


class BundleInvalid(ValueError):
    pass


def validate_bundle(bundle: dict) -> None:
    """Raise BundleInvalid with a readable list of problems."""
    problems: list[str] = []
    try:
        Bundle.model_validate(bundle)
    except ValidationError as e:
        for err in e.errors()[:10]:
            problems.append(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}")
    if bundle.get("type") != "transaction":
        problems.append(f"Bundle.type = {bundle.get('type')!r}, expected 'transaction'")
    for i, entry in enumerate(bundle.get("entry", [])):
        method = (entry.get("request") or {}).get("method")
        if method not in HTTP_VERBS:
            problems.append(f"entry[{i}].request.method = {method!r} is not in the required value set")
        res = entry.get("resource") or {}
        rt = res.get("resourceType", "")
        for element, allowed in REQUIRED_BINDINGS.get(rt, {}).items():
            if element in res and res[element] not in allowed:
                problems.append(f"entry[{i}] {rt}.{element} = {res[element]!r} is not in the required value set")
        for parent in ("name", "telecom", "address", "identifier", "link"):
            for item in res.get(parent, []) if isinstance(res.get(parent), list) else []:
                for key in ("use", "system", "type"):
                    allowed = NESTED_BINDINGS.get(f"{key}@{parent}")
                    if allowed and isinstance(item.get(key), str) and item[key] not in allowed:
                        problems.append(f"entry[{i}] {rt}.{parent}.{key} = {item[key]!r} is not in the required value set")
        if rt == "Parameters" and entry.get("request", {}).get("method") == "PATCH":
            for p in res.get("parameter", []):
                for part in p.get("part", []):
                    if part.get("name") == "value" and "valueCode" in part and part["valueCode"] not in SERVICE_REQUEST_STATUS:
                        problems.append(f"entry[{i}] PATCH value {part['valueCode']!r} is not a ServiceRequest status")
    if problems:
        raise BundleInvalid("; ".join(problems))
