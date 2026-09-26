"""Where bundles go: a directory (always safe, useful as a spool and for diffing) and/or a FHIR server."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx


def safe_name(*parts: str) -> str:
    """A file name built from sender-controlled text (MSH-3, MSH-10). '../../etc' is data, not a path."""
    raw = "_".join(parts)
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", raw).strip("_")[:80] or "message"
    return f"{clean}-{hashlib.sha256(raw.encode('utf-8', 'surrogatepass')).hexdigest()[:10]}"


class DirectorySink:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, bundle: dict, name: str) -> Path:
        path = self.root / f"{name}.json"
        tmp = path.with_suffix(".json.part")
        tmp.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)            # atomic: a reader never sees half a bundle
        return path


class FhirError(Exception):
    def __init__(self, text: str, status: int | None = None, retryable: bool = False):
        super().__init__(text)
        self.status = status
        self.retryable = retryable


@dataclass
class EntryResult:
    resource_type: str
    status: str             # "201 Created", "200 OK", ...
    location: str
    outcome: str            # created | updated | unchanged | matched | patched | ok

    @property
    def label(self) -> str:
        return {"created": "+", "updated": "~", "patched": "~"}.get(self.outcome, "=")


class FhirSink:
    """POSTs a transaction bundle to <base_url>. Retries connection errors and 5xx with backoff; never 4xx
    (the same bundle would be refused again). `deadline` (a time.monotonic() value) caps every request and
    stops retrying, so the sender gets its ACK before its own timeout. Callers serialise access (Bridge)."""

    def __init__(self, base_url: str, timeout: float = 15.0, retries: int = 2, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout, transport=transport,
                                    headers={"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"})

    def close(self) -> None:
        self._client.close()

    def _timeout(self, deadline: float | None) -> float:
        if deadline is None:
            return self.timeout
        remaining = deadline - time.monotonic()
        if remaining < 0.5:
            raise FhirError("ACK deadline reached before the FHIR server answered", None, True)
        return min(self.timeout, remaining)

    def search(self, query: str, deadline: float | None = None) -> list[dict]:
        """GET <base>/<Type>?<params>; returns the matching resources. Raises FhirError."""
        try:
            r = self._client.get(f"{self.base_url}/{query}", timeout=self._timeout(deadline))
        except httpx.HTTPError as e:
            raise FhirError(f"FHIR server unreachable: {type(e).__name__}: {e}", None, True) from None
        if r.status_code >= 400:
            raise FhirError(f"FHIR search failed ({r.status_code}): {_diagnostics(r)}", r.status_code, r.status_code >= 500)
        return [e["resource"] for e in r.json().get("entry", []) if "resource" in e]

    def post(self, bundle: dict, deadline: float | None = None) -> list[EntryResult]:
        body = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
        last: FhirError | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                backoff = min(0.25 * 2 ** attempt, 4)
                if deadline is not None and deadline - time.monotonic() <= backoff + 0.5:
                    break                          # no time left for another try before the ACK is due
                time.sleep(backoff)
            try:
                r = self._client.post(self.base_url + "/", content=body, timeout=self._timeout(deadline))
            except httpx.HTTPError as e:
                last = FhirError(f"FHIR server unreachable: {type(e).__name__}: {e}", None, True)
                continue
            if r.status_code >= 500:
                last = FhirError(f"FHIR server error {r.status_code}: {_diagnostics(r)}", r.status_code, True)
                continue
            if r.status_code >= 400:
                raise FhirError(f"FHIR server refused the bundle ({r.status_code}): {_diagnostics(r)}", r.status_code, False)
            return _entry_results(bundle, r.json())
        raise last  # type: ignore[misc]


def _diagnostics(r: httpx.Response) -> str:
    try:
        oo = r.json()
        return "; ".join(i.get("diagnostics") or i.get("details", {}).get("text", "") for i in oo.get("issue", []))[:300] or r.text[:300]
    except ValueError:
        return r.text[:300]


def _entry_results(request: dict, response: dict) -> list[EntryResult]:
    if response.get("resourceType") != "Bundle" or response.get("type") != "transaction-response":
        raise FhirError("FHIR server did not answer with a transaction-response bundle", None, False)
    out = []
    for req, resp in zip(request.get("entry", []), response.get("entry", []), strict=False):
        rt = req["request"]["url"].split("?")[0].split("/")[0]
        r = resp.get("response", {})
        status = r.get("status", "")
        outcome = "created" if status.startswith("201") else "ok"
        for issue in (r.get("outcome") or {}).get("issue", []):
            for c in issue.get("details", {}).get("coding", []):
                if c.get("system") == "urn:mock-fhir:outcome":
                    outcome = c.get("code", outcome)
        out.append(EntryResult(rt, status, r.get("location", ""), outcome))
    return out
