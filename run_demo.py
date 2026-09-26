"""One command, whole workflow: mock FHIR server + MLLP listener + a simulated RIS and reporting system.

  python run_demo.py                                  # mock FHIR server, bundles in ./data/bundles
  python run_demo.py --fhir-url http://localhost:8080/fhir   # a real server instead (e.g. HAPI FHIR)

The RIS sends, over MLLP, one patient's radiology workflow:
  A04 register -> ORM NW -> ORM SC (exam complete) -> ORU prelim -> ORU final -> ORU corrected
  -> A08 name change -> second order NW -> cancel of that order (ORC only)
then two things interface engines really do: resend a message it already sent, and send garbage.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

import httpx

from mock_fhir import MockFhirServer
from tools.ris_sim import demo_script
from v2fhir.bridge import Bridge, Result
from v2fhir.config import load_config
from v2fhir.mllp import MLLPClient, MLLPServer, ServerThread, bridge_handler

HERE = Path(__file__).resolve().parent


def main(argv=None) -> list[dict]:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="data")
    ap.add_argument("--fhir-url", default="", help="use this FHIR server instead of the built-in mock")
    ap.add_argument("--port", type=int, default=0, help="MLLP port (0 = any free port)")
    ap.add_argument("--keep", action="store_true", help="do not wipe the workdir first")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")

    work = Path(a.workdir)
    if work.exists() and not a.keep:
        shutil.rmtree(work)
    mock = None if a.fhir_url else MockFhirServer().start()
    base_url = a.fhir_url or mock.base_url
    cfg = load_config(HERE / "config" / "bridge.toml", {"out_dir": str(work / "bundles"), "fhir_base_url": base_url, "port": a.port})

    results: list[Result] = []
    bridge = Bridge(cfg, on_result=results.append)
    listener = ServerThread(MLLPServer(bridge_handler(bridge), cfg.host, cfg.port, cfg.max_message_bytes)).start()
    try:
        return _run(a, cfg, base_url, listener.port, results, mock is not None)
    finally:
        listener.stop()
        bridge.close()
        if mock:
            mock.stop()


def _run(a, cfg, base_url: str, port: int, results: list[Result], is_mock: bool) -> list[dict]:
    print(f"\n[1/3] FHIR server {'(mock) ' if is_mock else ''}at {base_url}; MLLP listener on {cfg.host}:{port}")
    script = demo_script()
    steps = [(label, m.encode("utf-8")) for label, m in script]
    steps.append(("engine resends #2 (same MSH-10)", steps[1][1]))
    steps.append(("garbage in an MLLP frame", b"this is not HL7\r"))
    print(f"[2/3] simulated RIS sending {len(steps)} messages over MLLP\n")

    rows = []
    with MLLPClient(cfg.host, port) as ris:
        for label, payload in steps:
            before = len(results)
            ack = ris.send(payload).decode("utf-8", errors="replace")
            r = results[before] if len(results) > before else None
            msa = next((s.split("|") for s in ack.split("\r") if s.startswith("MSA")), ["MSA", "?", ""])
            rows.append({"step": label, "type": (r.message_type if r else "") or "?", "control_id": (r.control_id if r else "") or "?",
                         "ack": msa[1], "ack_control_id": msa[2] if len(msa) > 2 else "",
                         "entries": [(e.resource_type, e.outcome) for e in (r.entries if r else [])],
                         "issues": [str(i) for i in (r.issues if r else [])], "duplicate": bool(r and r.duplicate)})

    print(f"  {'#':>2}  {'step':34s} {'message':9s} {'control id':11s} {'ACK':4s} FHIR  (+ created  ~ updated  = already there; +1 Provenance each)")
    for i, row in enumerate(rows, 1):
        if row["duplicate"]:
            fhir = "duplicate: acknowledged, not sent again"
        elif row["ack"] != "AA":
            fhir = row["issues"][0][:60] if row["issues"] else ""
        else:
            fhir = " ".join(("+" if o == "created" else "~" if o in ("updated", "patched") else "=") + t
                            for t, o in row["entries"] if t != "Provenance")
            warn = [w for w in row["issues"] if "without UTC offset" not in w]
            if warn:
                fhir += f"   [warning: {warn[0][:50]}]"
        print(f"  {i:>2}  {row['step']:34s} {row['type']:9s} {row['control_id']:11s} {row['ack']:4s} {fhir}")

    print("\n[3/3] FHIR server state")
    state = _summarise(base_url)
    bundles = sorted((Path(a.workdir) / "bundles").glob("*.json"))
    print(f"\n  {len(bundles)} transaction bundles written to {Path(a.workdir) / 'bundles'}")
    print("  timestamps sent without a UTC offset were read as America/Chicago (config: default_timezone)\n")
    if is_mock:
        _check(state, rows)
    return rows


def _get(client: httpx.Client, base: str, path: str) -> list[dict]:
    r = client.get(f"{base}/{path}")
    r.raise_for_status()
    return [e["resource"] for e in r.json().get("entry", [])]


def _summarise(base: str) -> dict:
    state: dict = {}
    with httpx.Client(timeout=15, trust_env=False) as c:      # the in-process mock, never via a proxy
        for rt in ("Patient", "Encounter", "Practitioner", "ServiceRequest", "ImagingStudy", "DiagnosticReport", "Provenance"):
            state[rt] = _get(c, base, rt)
        state["dr_history"] = {dr["id"]: _get(c, base, f"DiagnosticReport/{dr['id']}/_history") for dr in state["DiagnosticReport"]}
    for p in state["Patient"]:
        n = p.get("name", [{}])[0]
        mrn = next((i["value"] for i in p.get("identifier", []) if i.get("use") == "usual"), "?")
        print(f"  {'Patient':17s} {n.get('family', '')}, {' '.join(n.get('given', []))}   MRN {mrn}   version {p['meta']['versionId']}")
    for sr in state["ServiceRequest"]:
        acc = next((i["value"] for i in sr["identifier"] if i["type"]["coding"][0]["code"] == "ACSN"), sr["identifier"][0]["value"])
        print(f"  {'ServiceRequest':17s} {acc}  {sr.get('code', {}).get('text', ''):26s} status {sr['status']:10s} version {sr['meta']['versionId']}")
    for dr in state["DiagnosticReport"]:
        hist = " -> ".join(v["status"] for v in reversed(state["dr_history"][dr["id"]]))
        print(f"  {'DiagnosticReport':17s} {hist}   ({dr['basedOn'][0]['reference']}, {dr.get('imagingStudy', [{}])[0].get('reference', 'no study')})")
        print(f"  {'':17s} conclusion: {dr.get('conclusion', '')[:70]}")
    for s in state["ImagingStudy"]:
        uid = next(i["value"] for i in s["identifier"] if i.get("system") == "urn:dicom:uid")
        print(f"  {'ImagingStudy':17s} {uid[:40]}...  status {s['status']}")
    print("  " + ", ".join(f"{rt} {len(state[rt])}" for rt in ("Encounter", "Practitioner", "Provenance")))
    return state


def _check(state: dict, rows: list[dict]) -> None:
    """The demo is also a test (CI runs it): fail loudly if the end state is wrong."""
    problems = []
    if [r["ack"] for r in rows] != ["AA"] * (len(rows) - 1) + ["AR"]:
        problems.append(f"unexpected ACKs {[r['ack'] for r in rows]}")
    if len(state["Patient"]) != 1 or state["Patient"][0]["name"][0]["family"] != "NÚÑEZ-GARCÍA":
        problems.append("expected one patient, renamed by the A08")
    statuses = sorted(sr["status"] for sr in state["ServiceRequest"])
    if statuses != ["completed", "revoked"]:
        problems.append(f"expected orders completed + revoked, got {statuses}")
    hist = [[v["status"] for v in reversed(h)] for h in state["dr_history"].values()]
    if hist != [["preliminary", "final", "corrected"]]:
        problems.append(f"expected one report preliminary -> final -> corrected, got {hist}")
    if len(state["ImagingStudy"]) != 1 or len(state["Provenance"]) != 9:
        problems.append("expected 1 ImagingStudy and 9 Provenance")
    if problems:
        raise SystemExit("DEMO CHECK FAILED: " + "; ".join(problems))


if __name__ == "__main__":
    main()
