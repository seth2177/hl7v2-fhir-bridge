"""A tiny in-memory FHIR R4 server: just enough to execute the bridge's transaction bundles for real.

Supports what the bridge uses, with the semantics of FHIR R4 http.html:
  POST /                      transaction Bundle, all-or-nothing
      POST + ifNoneExist      conditional create: 0 matches create, 1 match = no-op, >1 = 412
      PUT Type?identifier=..  conditional update: 0 create, 1 update, >1 = 412
      PUT Type/id             update-as-create with a client id
      PATCH Type?identifier=  FHIRPath Patch (replace/add on top-level elements): 0 matches = 404
      urn:uuid references     rewritten to the ids the conditionals resolved to
  GET  /Type?identifier=sys|value, /Type, /Type/id, /Type/id/_history, /metadata

Like HAPI, an update that changes nothing keeps the same version. Each response entry carries an
OperationOutcome coded created | updated | unchanged | matched | patched (system urn:mock-fhir:outcome) so
the demo can show exactly what happened. It is a test double, not a FHIR server: no auth, no validation
beyond resourceType/id sanity, no search beyond identifier and _id.
"""
from __future__ import annotations

import copy
import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

OUTCOME_SYSTEM = "urn:mock-fhir:outcome"
ID_RX = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
TYPE_RX = re.compile(r"^[A-Z][A-Za-z]+$")


class TxError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


def _split(s: str, sep: str, unescape: bool) -> list[str]:
    """Split on unescaped `sep`; optionally undo FHIR search escaping (\\| \\, \\$ \\\\)."""
    parts, cur, i = [], [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            cur.append(s[i + 1] if unescape else s[i:i + 2])
            i += 2
            continue
        if c == sep:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return parts


def _unescape_token(s: str) -> list[str]:
    return _split(s, "|", True)


def _outcome(code: str) -> dict:
    return {"resourceType": "OperationOutcome", "issue": [{"severity": "information", "code": "informational",
                                                            "details": {"coding": [{"system": OUTCOME_SYSTEM, "code": code}]}}]}


def _error_outcome(text: str) -> dict:
    return {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "processing", "diagnostics": text}]}


class FhirStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.data: dict[str, dict[str, list[dict]]] = {}    # type -> id -> [versions]
        self._next = 1

    # ---- read -----------------------------------------------------------------------------------
    def current(self, rtype: str, rid: str) -> dict | None:
        versions = self.data.get(rtype, {}).get(rid)
        return copy.deepcopy(versions[-1]) if versions else None

    def all(self, rtype: str) -> list[dict]:
        return [copy.deepcopy(v[-1]) for v in self.data.get(rtype, {}).values()]

    def history(self, rtype: str, rid: str) -> list[dict]:
        return copy.deepcopy(self.data.get(rtype, {}).get(rid, []))

    def counts(self) -> dict[str, int]:
        return {t: len(ids) for t, ids in sorted(self.data.items())}

    def search(self, rtype: str, params: list[tuple[str, str]], data=None) -> list[str]:
        data = self.data if data is None else data
        ids = list(data.get(rtype, {}).keys())
        for name, value in params:
            if name == "identifier":                       # a,b,c = any of them (OR)
                wanted = []
                for tok in _split(value, ",", False):
                    parts = _unescape_token(tok)
                    wanted.append((parts[0], parts[1]) if len(parts) > 1 else (None, parts[0]))
                ids = [i for i in ids if any((system is None or (idf.get("system") or "") == system) and idf.get("value") == val
                                             for system, val in wanted for idf in data[rtype][i][-1].get("identifier", []))]
            elif name == "_id":
                ids = [i for i in ids if i == value]
            else:
                raise TxError(400, f"search parameter {name!r} not supported by the mock")
        return ids

    # ---- transaction ---------------------------------------------------------------------------
    def transaction(self, bundle: dict) -> dict:
        if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "transaction":
            raise TxError(400, "expected a transaction Bundle")
        with self.lock:
            work = copy.deepcopy(self.data)
            next_id = self._next
            plan = []
            mapping: dict[str, str] = {}
            claimed: set[tuple[str, str]] = set()
            for i, entry in enumerate(bundle.get("entry", [])):
                req = entry.get("request") or {}
                method, url = req.get("method"), req.get("url", "")
                res = entry.get("resource")
                path, _, query = url.partition("?")
                rtype = path.split("/")[0]
                if not TYPE_RX.match(rtype):
                    raise TxError(400, f"entry {i}: bad url {url!r}")
                params = parse_qsl(query, keep_blank_values=True)
                if method == "POST":
                    self._check_type(res, rtype, i)
                    matches = self.search(rtype, parse_qsl(req.get("ifNoneExist", ""), keep_blank_values=True), work) if req.get("ifNoneExist") else []
                    if len(matches) > 1:
                        raise TxError(412, f"entry {i}: ifNoneExist matched {len(matches)} {rtype} resources")
                    if matches:
                        rid, action = matches[0], "matched"
                    else:
                        rid, next_id, action = str(next_id), next_id + 1, "create"
                elif method in ("PUT", "PATCH"):
                    if method == "PUT":
                        self._check_type(res, rtype, i)
                    if query:
                        matches = self.search(rtype, params, work)
                        if len(matches) > 1:
                            raise TxError(412, f"entry {i}: conditional {method} matched {len(matches)} {rtype} resources")
                        if not matches and method == "PATCH":
                            raise TxError(404, f"entry {i}: conditional PATCH {url} matched no resource")
                        if matches:
                            rid, action = matches[0], ("update" if method == "PUT" else "patch")
                        else:
                            rid, next_id, action = str(next_id), next_id + 1, "create"
                    else:
                        rid = path.split("/")[1] if "/" in path else ""
                        if not ID_RX.match(rid):
                            raise TxError(400, f"entry {i}: bad id in {url!r}")
                        exists = rid in work.get(rtype, {})
                        if method == "PATCH" and not exists:
                            raise TxError(404, f"entry {i}: {url} not found")
                        action = ("update" if exists else "create") if method == "PUT" else "patch"
                else:
                    raise TxError(400, f"entry {i}: method {method!r} not supported by the mock")
                key = (rtype, rid)
                if key in claimed:
                    raise TxError(400, f"entry {i}: {rtype}/{rid} is the target of two entries in one transaction")
                claimed.add(key)
                if entry.get("fullUrl"):
                    mapping[entry["fullUrl"]] = f"{rtype}/{rid}"
                plan.append((i, method, rtype, rid, action, res))

            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            responses = []
            for i, _method, rtype, rid, action, res in plan:
                versions = work.setdefault(rtype, {}).setdefault(rid, [])
                if action == "matched":
                    responses.append(self._resp("200 OK", rtype, rid, len(versions), "matched"))
                    continue
                if action == "patch":
                    new = _apply_patch(copy.deepcopy(versions[-1]), res, i)
                else:
                    new = _rewrite(copy.deepcopy(res), mapping)
                new["id"] = rid
                if versions and _same(new, versions[-1]):
                    responses.append(self._resp("200 OK", rtype, rid, len(versions), "unchanged"))
                    continue
                meta = dict(new.get("meta") or {})
                meta.update({"versionId": str(len(versions) + 1), "lastUpdated": now})
                new["meta"] = meta
                versions.append(new)
                if action == "create":
                    responses.append(self._resp("201 Created", rtype, rid, len(versions), "created"))
                else:
                    responses.append(self._resp("200 OK", rtype, rid, len(versions), "patched" if action == "patch" else "updated"))
            self.data, self._next = work, next_id
        return {"resourceType": "Bundle", "type": "transaction-response", "entry": responses}

    @staticmethod
    def _check_type(res, rtype: str, i: int) -> None:
        if not isinstance(res, dict) or res.get("resourceType") != rtype:
            raise TxError(400, f"entry {i}: resource type does not match request url {rtype}")

    @staticmethod
    def _resp(status: str, rtype: str, rid: str, version: int, code: str) -> dict:
        return {"response": {"status": status, "location": f"{rtype}/{rid}/_history/{version}", "etag": f'W/"{version}"',
                             "outcome": _outcome(code)}}


def _same(a: dict, b: dict) -> bool:
    """No-op detection: same content, ignoring meta (a replay from a new control id only changes meta.source)."""
    return {k: v for k, v in a.items() if k != "meta"} == {k: v for k, v in b.items() if k != "meta"}


def _rewrite(obj, mapping: dict[str, str]):
    if isinstance(obj, dict):
        return {k: (mapping.get(v, v) if k == "reference" and isinstance(v, str) else _rewrite(v, mapping)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rewrite(v, mapping) for v in obj]
    return obj


def _apply_patch(resource: dict, params: dict, i: int) -> dict:
    """FHIRPath Patch, top-level elements only: replace (path Type.element) and add (path Type, name)."""
    if not isinstance(params, dict) or params.get("resourceType") != "Parameters":
        raise TxError(400, f"entry {i}: PATCH body must be a Parameters resource")
    for op in params.get("parameter", []):
        if op.get("name") != "operation":
            continue
        parts = {p.get("name"): p for p in op.get("part", [])}
        kind = parts.get("type", {}).get("valueCode")
        path = parts.get("path", {}).get("valueString", "")
        value_part = parts.get("value", {})
        value = next((v for k, v in value_part.items() if k.startswith("value")), None)
        segs = path.split(".")
        if segs[0] != resource.get("resourceType"):
            raise TxError(400, f"entry {i}: patch path {path!r} does not start with {resource.get('resourceType')}")
        if kind == "replace" and len(segs) == 2:
            if segs[1] not in resource:
                raise TxError(400, f"entry {i}: replace on missing element {path!r}")
            resource[segs[1]] = value
        elif kind == "add" and len(segs) == 1:
            resource[parts.get("name", {}).get("valueString", "")] = value
        else:
            raise TxError(400, f"entry {i}: patch operation {kind!r} on {path!r} not supported by the mock")
    return resource


class _Handler(BaseHTTPRequestHandler):
    store: FhirStore
    max_body = 64 * 1024 * 1024
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/fhir+json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length > self.max_body:
            return self._send(413, _error_outcome("body too large"))
        body = self.rfile.read(length)
        if urlsplit(self.path).path.rstrip("/") not in ("", "/fhir"):
            return self._send(405, _error_outcome("only transaction bundles are accepted, POST them to the base url"))
        try:
            bundle = json.loads(body)
            return self._send(200, self.store.transaction(bundle))
        except TxError as e:
            return self._send(e.status, _error_outcome(str(e)))
        except (ValueError, TypeError, AttributeError, KeyError) as e:
            return self._send(400, _error_outcome(f"bad request: {e}"))

    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        segs = [s for s in parts.path.split("/") if s and s != "fhir"]
        if segs == ["metadata"]:
            return self._send(200, {"resourceType": "CapabilityStatement", "status": "active", "kind": "instance", "fhirVersion": "4.0.1",
                                    "format": ["json"], "date": "2026-01-01", "software": {"name": "mock_fhir (test double)"}})
        try:
            if len(segs) == 1:
                ids = self.store.search(segs[0], parse_qsl(parts.query, keep_blank_values=True))
                entries = [{"resource": self.store.current(segs[0], i)} for i in ids]
                return self._send(200, {"resourceType": "Bundle", "type": "searchset", "total": len(entries), "entry": entries})
            if len(segs) == 2:
                r = self.store.current(*segs)
                return self._send(200, r) if r else self._send(404, _error_outcome("not found"))
            if len(segs) == 3 and segs[2] == "_history":
                h = self.store.history(segs[0], segs[1])
                return self._send(200, {"resourceType": "Bundle", "type": "history", "total": len(h),
                                        "entry": [{"resource": r} for r in reversed(h)]})
        except TxError as e:
            return self._send(e.status, _error_outcome(str(e)))
        return self._send(404, _error_outcome("not found"))


class MockFhirServer:
    """Run the mock on a background thread. port=0 picks a free port."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, store: FhirStore | None = None):
        self.store = store or FhirStore()
        handler = type("Handler", (_Handler,), {"store": self.store})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True, name="mock-fhir")

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/fhir"

    def start(self) -> MockFhirServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
