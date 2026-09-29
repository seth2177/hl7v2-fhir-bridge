"""Attacking my own code.

Regressions: the first four tests are problems the fuzzer or my review found; each one fails on the code
before its fix. Then two tests for ambiguous order numbers and search-special characters, and the
fuzzing itself, seeded so CI is repeatable."""
import logging
import os
import random
import socket
import threading

import pytest

from tests.conftest import SAMPLES, to_bundle
from v2fhir.bridge import Bridge
from v2fhir.convert import convert
from v2fhir.errors import HL7Error
from v2fhir.hl7.ack import head_fields
from v2fhir.hl7.parser import parse_bytes
from v2fhir.mllp import FrameDecoder, MLLPClient, MLLPServer, ServerThread, bridge_handler, frame
from v2fhir.tools import ris_sim as r
from v2fhir.validate import validate_bundle

O1 = dict(placer="ORD1001", filler="FIL5001", accession="ACC2001", procedure=r.CT_CHEST, modality="CT")


# ---- regressions ----------------------------------------------------------------------------------
def test_ack_never_echoes_mllp_control_bytes(cfg):
    # Found by fuzzing: MSH-3 containing 0x0B / 0x1C was copied into the ACK's MSH-5. A 0x1C followed by the
    # segment CR is an MLLP end-of-frame: the sender would read a truncated ACK.
    raw = r.adt("A04", "C\x1c1").replace("RIS_SIM", "RIS\x0bSIM\x1c").encode()
    res = Bridge(cfg).handle(raw)
    assert b"\x0b" not in res.ack_bytes and b"\x1c" not in res.ack_bytes
    assert res.ack.startswith("MSH|^~\\&|V2FHIR|BRIDGE|RISSIM|")


def test_garbage_segment_id_is_not_echoed_into_err(cfg):
    # Found by fuzzing: the "segment id" of a broken line (e.g. '9|^') went into ERR-2 and split the ERR segment.
    res = Bridge(cfg).handle(r.adt("A04", "C2").replace("EVN|", "9|^A|").encode())
    err = next(s for s in res.ack.split("\r") if s.startswith("ERR"))
    assert res.ack_code == "AR" and err.split("|")[2] == "" and err.split("|")[3].startswith("100^")


def test_control_character_field_separator_is_rejected(cfg):
    # Found by fuzzing: "MSH\x00^~\\&\x00..." was accepted with NUL as the field separator, and the ACK
    # was then built with NUL separators.
    res = Bridge(cfg).handle(b"MSH\x00^~\\&\x00A\x00B\x00C\x00D\x002026\x00\x00ADT^A04\x00X1\x00P\x002.5\r")
    assert res.ack_code == "AR" and res.ack.startswith("MSH|^~\\&|") and "\rMSA|AR|" in res.ack


def test_result_that_does_not_echo_the_placer_number_finds_its_order(bridge):
    # Found in review: the order was matched on ONE number ("placer, else filler, else accession"). The NW
    # comes from the HIS before the RIS has assigned a filler number; the report comes back with filler +
    # accession but no placer. The old code searched on the filler only, found nothing, and created a
    # second ServiceRequest with the report based on it.
    nw = r.orm("NW", "O1", **O1).replace("FIL5001^SYNTH_RIS", "")
    assert bridge.handle(nw.encode()).ack_code == "AA"
    oru = r.oru("F", "R1", **O1, findings=["a"], impression="b").replace("ORD1001^SYNTH_HIS", "")
    assert bridge.handle(oru.encode()).ack_code == "AA"
    s = bridge.server.store
    [sr] = s.all("ServiceRequest")
    assert s.all("DiagnosticReport")[0]["basedOn"] == [{"reference": f"ServiceRequest/{sr['id']}"}]


def test_numbers_pointing_at_two_different_orders_are_refused_not_guessed(bridge):
    bridge.handle(r.orm("NW", "O1", placer="P1", filler="F1", accession="A1", procedure=r.CT_CHEST, modality="CT").encode())
    bridge.handle(r.orm("NW", "O2", placer="P2", filler="F2", accession="A2", procedure=r.CT_CHEST, modality="CT").encode())
    res = bridge.handle(r.oru("F", "R1", placer="P1", filler="F2", accession="A9", procedure=r.CT_CHEST, modality="CT",
                              findings=["a"], impression="b").encode())
    assert res.ack_code == "AE" and "412" in res.issues[0].text
    assert "DiagnosticReport" not in bridge.server.store.counts()


def test_identifier_with_search_special_characters_is_idempotent(bridge):
    ids = "12\\F\\34,5$6\\E\\7^^^SYNTH_HOSP^MR"                     # value 12|34,5$6\7
    for ctrl in ("I1", "I2"):
        assert bridge.handle(r.adt("A04", ctrl).replace(r.PATIENT["ids"], ids).encode()).ack_code == "AA"
    [p] = bridge.server.store.all("Patient")
    assert p["identifier"][0]["value"] == "12|34,5$6\\7" and p["meta"]["versionId"] == "1"


# ---- fuzzing ----------------------------------------------------------------------------------------
SEEDS = [p.read_bytes() for p in sorted(SAMPLES.glob("*.hl7"))]
ALPHABET = b"|^~\\&\r\n\x0b\x1c\"\x00 AZ09.-" + "é".encode() + b"\xff"


def mutate(rng: random.Random, b: bytes) -> bytes:
    b = bytearray(b)
    for _ in range(rng.randint(1, 6)):
        if not b:
            b = bytearray(b"MSH")
        i = rng.randrange(len(b))
        op = rng.randrange(6)
        if op == 0:
            del b[i]
        elif op == 1:
            b.insert(i, rng.choice(ALPHABET))
        elif op == 2:
            b = b[:i]                                                        # truncated frame
        elif op == 3:
            b[i] = rng.randrange(256)
        elif op == 4:
            segs = bytes(b).split(b"\r")
            segs.insert(rng.randrange(len(segs)), segs[rng.randrange(len(segs))])   # duplicated segment
            b = bytearray(b"\r".join(segs))
        else:
            b[i:i] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 20)))
    return bytes(b)


@pytest.fixture(autouse=True)
def quiet_logs():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def test_fuzz_parser_converter_and_bridge(cfg):
    """Seeded, so a failure reproduces. FUZZ_N=120000 python -m pytest tests/test_adversarial.py -k fuzz_parser
    runs the full-size pass (about a minute and a half); the suite runs 3,000."""
    rng = random.Random(20260925)
    bridge = Bridge(cfg)
    for n in range(int(os.environ.get("FUZZ_N", "3000"))):
        raw = mutate(rng, rng.choice(SEEDS)) if n % 4 else bytes(rng.randrange(256) for _ in range(rng.randint(0, 200)))
        try:
            msg = parse_bytes(raw)
            try:
                validate_bundle(convert(msg, cfg).bundle)    # anything that converts must be valid FHIR
            except HL7Error:
                pass
        except HL7Error:
            pass
        res = bridge.handle(raw)                            # must never raise
        if res.ack is not None:
            ack = res.ack_bytes
            fs = (head_fields(raw).get("fs") or "|").encode("latin-1")    # the ACK uses the sender's own separator
            assert ack.startswith(b"MSH" + fs) and b"\rMSA" + fs in ack and b"\x0b" not in ack and b"\x1c" not in ack, raw
            assert res.ack_code in ("AA", "AE", "AR")
            assert "internal error" not in " ".join(str(i) for i in res.issues), raw


def test_fuzz_frame_decoder_reassembly():
    rng = random.Random(7)
    for _ in range(300):
        payloads = [bytes(rng.choice(b"MSH|^~\\&ABC\r\x1c") for _ in range(rng.randint(1, 400))).replace(b"\x1c\r", b"\x1c.")
                    for _ in range(rng.randint(1, 5))]
        stream = b"".join((b"noise" if rng.random() < 0.3 else b"") + frame(p) for p in payloads)
        d, out, i = FrameDecoder(), [], 0
        while i < len(stream):
            step = rng.randint(1, 64)
            out += d.feed(stream[i:i + step])
            i += step
        assert [f.data for f in out] == payloads


def test_fuzz_listener_never_dies(cfg, tmp_path):
    cfg.out_dir = str(tmp_path)
    srv = ServerThread(MLLPServer(bridge_handler(Bridge(cfg)), "127.0.0.1", 0, max_message_bytes=50_000, idle_timeout=2)).start()
    rng = random.Random(99)

    def attacker(seed: int) -> None:
        rr = random.Random(seed)
        for _ in range(15):
            try:
                with socket.create_connection(("127.0.0.1", srv.port), timeout=3) as s:
                    kind = rr.randrange(4)
                    if kind == 0:
                        s.sendall(bytes(rr.randrange(256) for _ in range(rr.randint(1, 3000))))
                    elif kind == 1:
                        s.sendall(frame(mutate(rr, rr.choice(SEEDS))))
                        s.recv(65536)
                    elif kind == 2:
                        s.sendall(b"\x0b" + rr.choice(SEEDS)[: rr.randint(1, 200)])       # truncated, then hang up
                    else:
                        s.sendall(b"\x0b" + b"A" * 60_000 + b"\x1c\r")                   # oversize
                        s.recv(65536)
            except OSError:
                pass

    threads = [threading.Thread(target=attacker, args=(rng.randrange(10**6),)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    try:
        with MLLPClient("127.0.0.1", srv.port, timeout=10) as c:
            ack = c.send(r.adt("A04", "STILLUP")).decode()
        assert "MSA|AA|STILLUP" in ack
    finally:
        srv.stop()


def test_every_sample_and_demo_message_validates(cfg):
    for text in [p.read_bytes() for p in SAMPLES.glob("*.hl7")] + [m.encode() for _, m in r.demo_script()]:
        to_bundle(text, cfg)
