"""Command line.

Installed from PyPI, `hl7v2-fhir-bridge` is the same command as `python -m v2fhir`.

  python -m v2fhir demo                                   the whole workflow, synthetic traffic (= python run_demo.py)
  python -m v2fhir convert samples/oru_r01_final.hl7       print the FHIR transaction Bundle (JSON)
  python -m v2fhir serve --fhir-url http://localhost:8080/fhir   run the MLLP listener
  python -m v2fhir send samples/orm_o01_new.hl7 --port 2575      send a file over MLLP, print the ACK
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .bridge import Bridge
from .config import default_config_path, load_config
from .convert import convert
from .errors import HL7Error
from .hl7.parser import parse_bytes, split_batch_bytes
from .mllp import MLLPClient, MLLPServer, bridge_handler, loop_factory
from .validate import BundleInvalid, validate_bundle


def _config(path: str | None, **overrides):
    p = path or default_config_path()
    return load_config(p, {k: v for k, v in overrides.items() if v is not None})


def cmd_convert(a) -> int:
    cfg = _config(a.config)
    raw = Path(a.file).read_bytes()
    now = datetime.fromisoformat(a.now) if a.now else None
    bundles, rc = [], 0
    for message in ([raw] if a.single else split_batch_bytes(raw)):
        try:
            conv = convert(parse_bytes(message, cfg.default_charset, cfg.fallback_charsets), cfg, now)
            if a.validate:
                validate_bundle(conv.bundle)
            for w in conv.warnings:
                print(f"warning: {w}", file=sys.stderr)
            bundles.append(conv.bundle)
        except (HL7Error, BundleInvalid) as e:
            print(f"error: {e}", file=sys.stderr)
            rc = 1
    if bundles:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(bundles[0] if len(bundles) == 1 else bundles, indent=2, ensure_ascii=False))
    return rc


def cmd_serve(a) -> int:
    cfg = _config(a.config, port=a.port, host=a.host, fhir_base_url=a.fhir_url, out_dir=a.out)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bridge = Bridge(cfg)
    server = MLLPServer(bridge_handler(bridge), cfg.host, cfg.port, cfg.max_message_bytes, cfg.idle_timeout_seconds, cfg.max_connections)
    print(f"MLLP listener on {cfg.host}:{cfg.port}  ->  {cfg.fhir_base_url or '(no FHIR server)'}  +  {cfg.out_dir or '(no bundle dir)'}")
    try:
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(server.serve_forever())
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
    return 0


def cmd_send(a) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    with MLLPClient(a.host, a.port, timeout=a.timeout) as c:
        for message in split_batch_bytes(Path(a.file).read_bytes()):
            print(c.send(message).decode("utf-8", errors="replace").replace("\r", "\n"))
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["demo"]:
        from .demo import main as demo
        demo(argv[1:])
        return 0
    ap = argparse.ArgumentParser(prog="python -m v2fhir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="the whole workflow over MLLP with synthetic traffic (see demo --help)")
    c = sub.add_parser("convert", help="convert an HL7 v2 file to a FHIR transaction Bundle (stdout)")
    c.add_argument("file")
    c.add_argument("--config")
    c.add_argument("--no-validate", dest="validate", action="store_false")
    c.add_argument("--single", action="store_true", help="treat the file as one message (no batch split)")
    c.add_argument("--now", help="fixed conversion time (ISO 8601 with offset) for reproducible output")
    s = sub.add_parser("serve", help="run the MLLP listener")
    s.add_argument("--config")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--fhir-url")
    s.add_argument("--out")
    t = sub.add_parser("send", help="send an HL7 file over MLLP and print the ACK(s)")
    t.add_argument("file")
    t.add_argument("--host", default="127.0.0.1")
    t.add_argument("--port", type=int, default=2575)
    t.add_argument("--timeout", type=float, default=60, help="seconds to wait for each ACK")
    a = ap.parse_args(argv)
    return {"convert": cmd_convert, "serve": cmd_serve, "send": cmd_send}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
