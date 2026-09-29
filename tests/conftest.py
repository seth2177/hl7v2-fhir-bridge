from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from v2fhir.bridge import Bridge
from v2fhir.config import load_config
from v2fhir.convert import convert
from v2fhir.hl7.parser import parse, parse_bytes
from v2fhir.mock_fhir import MockFhirServer
from v2fhir.validate import validate_bundle

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
FIXED_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return load_config(ROOT / "config" / "bridge.toml", {"out_dir": "", "fhir_base_url": ""})


def sample_bytes(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


def to_bundle(message: str | bytes, cfg, validate: bool = True) -> dict:
    msg = parse_bytes(message) if isinstance(message, bytes) else parse(message)
    bundle = convert(msg, cfg, FIXED_NOW).bundle
    if validate:
        validate_bundle(bundle)
    return bundle


def resources(bundle: dict, rtype: str) -> list[dict]:
    return [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == rtype]


def entry(bundle: dict, rtype: str) -> dict:
    found = [e for e in bundle["entry"] if e["resource"]["resourceType"] == rtype]
    assert len(found) == 1, f"expected one {rtype} entry, got {len(found)}"
    return found[0]


@pytest.fixture
def mock_server():
    srv = MockFhirServer().start()
    yield srv
    srv.stop()


@pytest.fixture
def bridge(cfg, mock_server, tmp_path):
    cfg.fhir_base_url = mock_server.base_url
    cfg.out_dir = str(tmp_path / "bundles")
    cfg.fhir_retries = 0
    b = Bridge(cfg, clock=lambda: FIXED_NOW)
    b.server = mock_server
    yield b
    b.close()
