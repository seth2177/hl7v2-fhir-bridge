"""FHIR transaction Bundle builder with conditional (idempotent) requests.

Every resource is written in one of four ways, chosen by what the v2 message is allowed to assert:

  create-if-absent   POST  Type  + ifNoneExist: identifier=system|value
                     "Make sure this exists." A replay, or a second message about the same thing, matches
                     the existing resource and changes nothing.
  upsert             PUT   Type?identifier=system|value
                     "This message is the source of truth for this resource." Creates it or replaces it.
  patch              PATCH Type?identifier=system|value  (FHIRPath Patch, a Parameters resource)
                     "Change one element." Used when the message is too sparse to rebuild the resource.
  put-by-id          PUT   Type/<deterministic id>
                     For Provenance, which has no identifier to search on: the id is derived from MSH-10.

Inside the bundle, resources reference each other by `urn:uuid:` fullUrls. The server resolves each
conditional request first, then rewrites those references to the real ids (FHIR R4 http.html#trules).
The fullUrl is a UUIDv5 of the resource's key, so the same message always yields the same bundle.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

NS = uuid.UUID("9e4f5a0c-3b7d-4c1e-8a57-2c6a0b6f7d11")  # namespace for this project's UUIDv5 fullUrls


def token(system: str | None, value: str) -> str:
    """identifier search token, FHIR-escaped (\\ | , $) and URL-safe."""
    from urllib.parse import quote

    def esc(s: str) -> str:
        s = s.replace("\\", "\\\\").replace("|", "\\|").replace(",", "\\,").replace("$", "\\$")
        return quote(s, safe=":/._~-")
    return f"{esc(system)}|{esc(value)}" if system else esc(value)


def identifier_query(ident: dict, also: list[dict] | None = None) -> str:
    """identifier=sys|value, or identifier=a|1,b|2 (comma = OR in FHIR search) to match on any of several."""
    idents = [ident] + [i for i in (also or []) if i is not ident and (i.get("system"), i["value"]) != (ident.get("system"), ident["value"])]
    return "identifier=" + ",".join(token(i.get("system"), i["value"]) for i in idents)


@dataclass
class Entry:
    key: str
    full_url: str
    resource: dict
    method: str
    url: str
    if_none_exist: str | None = None
    resource_type: str = ""
    ident: dict | None = None

    def as_json(self) -> dict:
        req = {"method": self.method, "url": self.url}
        if self.if_none_exist:
            req["ifNoneExist"] = self.if_none_exist
        return {"fullUrl": self.full_url, "resource": self.resource, "request": req}


class TransactionBuilder:
    def __init__(self):
        self.entries: list[Entry] = []
        self._by_key: dict[str, Entry] = {}

    def _full_url(self, key: str) -> str:
        return f"urn:uuid:{uuid.uuid5(NS, key)}"

    def has(self, resource_type: str, ident: dict) -> bool:
        return f"{resource_type}|{ident.get('system')}|{ident['value']}" in self._by_key

    def url_for(self, resource_type: str, ident: dict) -> str:
        """The fullUrl a resource keyed on `ident` has (or will have) in this bundle."""
        return self._full_url(f"{resource_type}|{ident.get('system')}|{ident['value']}")

    def _add(self, key: str, resource: dict, method: str, url: str, if_none_exist: str | None = None) -> str:
        # The same resource twice in one transaction (e.g. the ordering provider in ORC-12 *and* OBR-16) is an
        # error on real servers, so the second mention reuses the first entry.
        existing = self._by_key.get(key)
        if existing:
            if existing.method == "POST" and method == "PUT":    # a stronger statement wins
                existing.method, existing.url, existing.if_none_exist, existing.resource = method, url, None, resource
            return existing.full_url
        e = Entry(key, self._full_url(key), resource, method, url, if_none_exist, key.split("|")[0].split("/")[0])
        self.entries.append(e)
        self._by_key[key] = e
        return e.full_url

    # `also`: other identifiers of the same thing. The match is on ANY of them, so an ORU that carries only
    # the filler number still finds the order the ORM created with placer + filler.
    def create_if_absent(self, resource: dict, ident: dict, also: list[dict] | None = None) -> str:
        rt = resource["resourceType"]
        key = f"{rt}|{ident.get('system')}|{ident['value']}"
        return self._add(key, resource, "POST", rt, identifier_query(ident, also))

    def upsert(self, resource: dict, ident: dict, also: list[dict] | None = None) -> str:
        rt = resource["resourceType"]
        key = f"{rt}|{ident.get('system')}|{ident['value']}"
        return self._add(key, resource, "PUT", f"{rt}?{identifier_query(ident, also)}")

    def patch(self, resource_type: str, ident: dict, operations: list[dict], also: list[dict] | None = None) -> str:
        key = f"{resource_type}|{ident.get('system')}|{ident['value']}"
        params = {"resourceType": "Parameters", "parameter": [{"name": "operation", "part": ops} for ops in operations]}
        url = self._add(key, params, "PATCH", f"{resource_type}?{identifier_query(ident, also)}")
        self._by_key[key].ident = ident
        return url

    def put_by_id(self, resource: dict) -> str:
        rt = resource["resourceType"]
        return self._add(f"{rt}/{resource['id']}", resource, "PUT", f"{rt}/{resource['id']}")

    def target_refs(self) -> list[dict]:
        """References to everything written so far (for Provenance.target). A PATCH entry has no resource
        of its own in the bundle, so it is referenced logically, by identifier."""
        out = []
        for e in self.entries:
            if e.method == "PATCH":
                out.append({"type": e.resource_type, "identifier": e.ident})
            else:
                out.append({"reference": e.full_url})
        return out

    def bundle(self, identifier: dict | None = None, timestamp: str | None = None) -> dict:
        b: dict = {"resourceType": "Bundle", "type": "transaction"}
        if identifier:
            b["identifier"] = identifier
        if timestamp:
            b["timestamp"] = timestamp
        b["entry"] = [e.as_json() for e in self.entries]
        return b


def replace_op(path: str, value_key: str, value) -> list[dict]:
    """One FHIRPath Patch 'replace' operation."""
    return [{"name": "type", "valueCode": "replace"}, {"name": "path", "valueString": path}, {"name": "value", value_key: value}]
