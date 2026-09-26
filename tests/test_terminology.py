"""Code maps checked against HL7 tables and the v2-to-FHIR IG 1.0.0 ConceptMaps (see docs/MAPPING.md)."""
import pytest

from tests.conftest import resources, sample_bytes, to_bundle
from v2fhir.convert import convert
from v2fhir.errors import HL7Error
from v2fhir.hl7.parser import parse, parse_bytes
from v2fhir.mapping import datatypes as dt

V2_0074 = "http://terminology.hl7.org/CodeSystem/v2-0074"


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


# ---- OBR-24 (table 0074) -> DiagnosticReport.category -------------------------------------------------
@pytest.mark.parametrize("obr24", ["MR", "US", "MG", "XA", "DX", "CR", "NM", "RF", "PT"])
def test_dicom_modality_in_obr24_is_not_written_as_a_0074_code(cfg, obr24):
    """MR, US, MG... are DICOM modalities, not table 0074 codes; 0074 PT is Physical Therapy, not PET."""
    category = _report(_oru(**{"|CT|F|": f"|{obr24}|F|"}), cfg)["category"][0]
    assert category["coding"][0]["code"] == "RAD" and category["text"] == obr24


@pytest.mark.parametrize("obr24", ["CT", "NMR", "RUS"])
def test_real_0074_codes_pass_through(cfg, obr24):
    assert _report(_oru(**{"|CT|F|": f"|{obr24}|F|"}), cfg)["category"][0] == {"coding": [{"system": V2_0074, "code": obr24}]}


def test_local_obr24_code_is_rad_with_a_warning(cfg):
    conv = convert(parse_bytes(_oru(**{"|CT|F|": "|XR|F|"})), cfg)
    assert resources(conv.bundle, "DiagnosticReport")[0]["category"][0]["text"] == "XR"
    assert any("not in table 0074" in str(w) for w in conv.warnings)


# ---- XPN-7 (table 0200) -> HumanName.use -------------------------------------------------------------
def _name_use(code: str) -> str | None:
    msg = parse(f"MSH|^~\\&|A|F|B|G|20260915080000||ADT^A04|X1|P|2.5.1\rPID|1||1^^^H^MR||REDCLOUD^CHIEF^^^^^{code}\r")
    return dt.xpn(msg.seg("PID").rep(5)).get("use")


@pytest.mark.parametrize("code, use", [("L", "official"), ("D", "usual"), ("M", "maiden"), ("TEMP", "temp"), ("BAD", "old"),
                                       ("T", None), ("A", None)])
def test_name_type_0200(code, use):
    """T is Indigenous/Tribal/Community name (not temporary) and A is Alias (not usual): the IG leaves both unmatched."""
    assert _name_use(code) == use
