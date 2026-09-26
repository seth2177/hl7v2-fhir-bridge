"""Code maps checked against HL7 tables and the v2-to-FHIR IG 1.0.0 ConceptMaps (see docs/MAPPING.md)."""
import pytest

from tests.conftest import resources, sample_bytes, to_bundle
from v2fhir.convert import convert
from v2fhir.errors import HL7Error
from v2fhir.hl7.parser import parse_bytes


def _oru(**replace: str) -> bytes:
    raw = sample_bytes("oru_r01_final.hl7")
    for old, new in replace.items():
        assert raw.count(old.encode()) == 1
        raw = raw.replace(old.encode(), new.encode())
    return raw


def _report(raw: bytes, cfg) -> dict:
    return resources(to_bundle(raw, cfg), "DiagnosticReport")[0]


# ---- OBR-25 (table 0123) -> DiagnosticReport.status --------------------------------------------------
def test_obr25_r_not_yet_verified_is_partial_not_preliminary(cfg):
    """Table 0123 R = "results stored; not yet verified". R4 preliminary = "verified early results"; the IG maps R -> partial."""
    assert _report(_oru(**{"|CT|F|": "|CT|R|"}), cfg)["status"] == "partial"


def test_obr25_d_is_not_a_table_0123_code(cfg):
    with pytest.raises(HL7Error) as exc:
        convert(parse_bytes(_oru(**{"|CT|F|": "|CT|D|"})), cfg)
    assert exc.value.issue.code == "103"
