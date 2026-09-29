"""MLLP framing and the asyncio listener over real TCP on localhost."""
import asyncio
import gc
import logging
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import v2fhir
from v2fhir.bridge import Bridge
from v2fhir.mllp import FrameDecoder, MLLPClient, MLLPServer, ServerThread, bridge_handler, frame
from v2fhir.tools import ris_sim as r

A = b"MSH|^~\\&|A|B|C|D|2026||ADT^A04|1|P|2.5\r"
B = b"MSH|^~\\&|A|B|C|D|2026||ADT^A04|2|P|2.5\r"


def test_one_frame():
    assert [f.data for f in FrameDecoder().feed(frame(A))] == [A]


def test_frame_split_byte_by_byte_including_between_fs_and_cr():
    d = FrameDecoder()
    out = []
    for b in frame(A) + frame(B):
        out += d.feed(bytes([b]))
    assert [f.data for f in out] == [A, B] and d.discarded == 0


def test_many_frames_in_one_read_and_partial_tail():
    d = FrameDecoder()
    data = frame(A) + frame(B) + frame(A)[:10]
    assert [f.data for f in d.feed(data)] == [A, B]
    assert [f.data for f in d.feed(frame(A)[10:])] == [A]


def test_bytes_outside_frames_are_discarded():
    d = FrameDecoder()
    assert [f.data for f in d.feed(b"\r\nnoise" + frame(A) + b"\x00\x00" + frame(B))] == [A, B]
    assert d.discarded == len(b"\r\nnoise") + 2


def test_fs_without_cr_is_data_and_new_start_abandons_partial_frame():
    d = FrameDecoder()
    assert [f.data for f in d.feed(b"\x0bab\x1cc" + frame(A))] == [A]           # ab<FS>c never ended: abandoned
    assert d.discarded == 4
    assert [f.data for f in FrameDecoder().feed(b"\x0bx\x1cy\x1c\r")] == [b"x\x1cy"]


def test_oversize_frame_keeps_head_only_and_bounded_memory():
    d = FrameDecoder(max_bytes=1000, head_bytes=64)
    big = A + b"OBX|1|TX|x||" + b"y" * 50_000 + b"\r"
    out = []
    for i in range(0, len(frame(big)), 999):
        out += d.feed(frame(big)[i:i + 999])
        assert len(d.buf) <= 1000 + 999
    assert len(out) == 1 and out[0].oversize and out[0].size == len(big) and out[0].data == big[:64]
    assert [f.data for f in d.feed(frame(A))] == [A]                          # decoder recovers


def test_large_message_under_the_limit_is_linear_time():
    big = A + b"OBX|1|TX|x||" + b"z" * 5_000_000 + b"\r"
    d = FrameDecoder(max_bytes=10_000_000)
    data = frame(big)
    t = time.perf_counter()
    out = []
    for i in range(0, len(data), 65536):
        out += d.feed(data[i:i + 65536])
    assert [f.data for f in out] == [big]
    assert time.perf_counter() - t < 5


# ---- listener -------------------------------------------------------------------------------------
@pytest.fixture
def listener(cfg, tmp_path):
    cfg.out_dir = str(tmp_path)
    bridge = Bridge(cfg)
    srv = ServerThread(MLLPServer(bridge_handler(bridge), "127.0.0.1", 0, max_message_bytes=200_000, idle_timeout=5)).start()
    yield srv
    srv.stop()


def msa(ack: bytes) -> list[str]:
    return next(s for s in ack.decode().split("\r") if s.startswith("MSA")).split("|")


def test_listener_acks_messages_split_across_writes(listener):
    payload = frame(r.adt("A04", "SPLIT1").encode())
    with socket.create_connection(("127.0.0.1", listener.port), timeout=10) as s:
        for i in range(0, len(payload), 7):
            s.sendall(payload[i:i + 7])
            time.sleep(0.001)
        d = FrameDecoder()
        acks = []
        while not acks:
            acks = d.feed(s.recv(4096))
    assert msa(acks[0].data)[:3] == ["MSA", "AA", "SPLIT1"]


def test_listener_two_messages_in_one_write_acked_in_order(listener):
    with MLLPClient("127.0.0.1", listener.port) as c:
        c.send_raw(frame(r.adt("A04", "M1").encode()) + frame(r.adt("A08", "M2").encode()))
        assert msa(c.recv())[2] == "M1" and msa(c.recv())[2] == "M2"


def test_listener_survives_garbage_and_disconnects(listener):
    for junk in (b"\x0bnot hl7\x1c\r", b"\x0b\x00\xff\xfe\x1c\r", b"\x0bMSH|^~\\&|A\x1c\r"):
        with MLLPClient("127.0.0.1", listener.port) as c:
            c.send_raw(junk)
            assert msa(c.recv())[1] == "AR"
    for junk in (b"\x0bMSH|^~\\&|half a message", b"random bytes, no frame", b""):     # hang up mid-frame
        s = socket.create_connection(("127.0.0.1", listener.port))
        s.sendall(junk)
        s.close()
    with MLLPClient("127.0.0.1", listener.port) as c:                           # still serving
        assert msa(c.send(r.adt("A04", "AFTER")))[:3] == ["MSA", "AA", "AFTER"]


def test_oversize_message_gets_ar_with_its_control_id(listener):
    big = r.adt("A04", "HUGE1") + "OBX|1|TX|x||" + "y" * 300_000 + "\r"
    with MLLPClient("127.0.0.1", listener.port) as c:
        m = msa(c.send(big))
        assert m[1] == "AR" and m[2] == "HUGE1" and "exceeds" in m[3]
        assert msa(c.send(r.adt("A04", "NEXT")))[1] == "AA"


def test_huge_but_allowed_report(cfg, tmp_path):
    cfg.out_dir = str(tmp_path)
    srv = ServerThread(MLLPServer(bridge_handler(Bridge(cfg)), "127.0.0.1", 0)).start()
    try:
        lines = ["Line %05d of a very long report." % i for i in range(60_000)]      # ~2 MB of OBX text
        msg = r.oru("F", "BIG1", placer="P1", filler="F1", accession="A1", procedure=r.CT_CHEST, modality="CT", findings=lines, impression="ok")
        with MLLPClient("127.0.0.1", srv.port, timeout=60) as c:
            assert msa(c.send(msg))[1] == "AA"
    finally:
        srv.stop()


def test_listener_survives_a_crashing_handler():
    calls = []

    def bad(fr):
        calls.append(fr)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return b"MSH|^~\\&|X\rMSA|AA|2\r"
    srv = ServerThread(MLLPServer(bad, "127.0.0.1", 0)).start()
    try:
        with MLLPClient("127.0.0.1", srv.port, timeout=5) as c:
            c.send_raw(frame(A))                         # no ACK for the crash, connection stays up
            assert c.send(B).startswith(b"MSH")
    finally:
        srv.stop()


def test_a_run_of_start_blocks_decodes_in_linear_time():
    """Every VT abandons the frame before it. The END search used to rescan the whole buffer for each one:
    64 KB of 0x0B took seconds on the event loop and stalled every other connection."""
    dec = FrameDecoder()
    t0 = time.perf_counter()
    assert dec.feed(b"\x0b" * 65536) == []
    assert time.perf_counter() - t0 < 0.5
    assert [f.data for f in FrameDecoder().feed(b"\x0b" * 1000 + frame(b"MSH|ok"))] == [b"MSH|ok"]


@pytest.mark.parametrize("split", [False, True])
def test_frame_of_exactly_max_bytes_is_accepted_however_it_arrives(split):
    payload = b"M" * 100
    dec = FrameDecoder(max_bytes=100)
    data = frame(payload)
    frames = dec.feed(data[:-1]) + dec.feed(data[-1:]) if split else dec.feed(data)
    assert [(f.data, f.oversize) for f in frames] == [(payload, False)]


# ---- shutdown ------------------------------------------------------------------------------------------------
def test_serve_forever_stops_when_cancelled_with_a_sender_still_connected():
    """Ctrl+C cancels serve_forever(). On Python 3.12+ asyncio.Server.serve_forever() then waited for every
    open connection to close, and an interface engine's connection never does."""
    async def main() -> bool:
        srv = MLLPServer(lambda fr: None, "127.0.0.1", 0, idle_timeout=30)
        await srv.start()
        task = asyncio.create_task(srv.serve_forever())
        _, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        await asyncio.sleep(0.2)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=3)
        writer.close()
        if not done:
            await srv.close()
        return bool(done)

    assert asyncio.run(main())


def test_stop_right_after_a_connect_is_prompt_and_leaves_no_pending_task(caplog):
    unraisable = []
    old_hook, sys.unraisablehook = sys.unraisablehook, unraisable.append
    caplog.set_level(logging.ERROR, logger="asyncio")
    try:
        durations = []
        for _ in range(5):
            srv = ServerThread(MLLPServer(lambda fr: None, "127.0.0.1", 0)).start()
            s = socket.create_connection(("127.0.0.1", srv.port))
            t = time.monotonic()
            srv.stop()
            durations.append(time.monotonic() - t)
            s.close()
        gc.collect()
    finally:
        sys.unraisablehook = old_hook
    assert max(durations) < 1.0
    assert not [r for r in caplog.records if "destroyed but it is pending" in r.getMessage()] and not unraisable


# ---- peers that hold resources: a stalled or silent peer is dropped, never the listener ------------------------
BIG_ACK = b"MSH|^~\\&|V2FHIR|BRIDGE\rMSA|AA|1\r" + b"ERR|" + b"x" * 400_000 + b"\r"


def test_connection_whose_peer_never_reads_acks_is_closed_after_idle_timeout():
    """drain() had no timeout, so a peer that sends but never reads held its connection task forever."""
    srv = ServerThread(MLLPServer(lambda fr: BIG_ACK, "127.0.0.1", 0, idle_timeout=1.0)).start()
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    s.connect(("127.0.0.1", srv.port))
    try:
        s.settimeout(2)
        for i in range(20):                               # sender keeps sending, never reads an ACK
            try:
                s.sendall(frame(b"MSH|^~\\&|A|B|C|D|2026||ADT^A04|%d|P|2.5\r" % i))
            except (TimeoutError, OSError):
                break
        time.sleep(5)                                     # 5x idle_timeout with no progress at all
        live = len(srv.server._tasks)
        assert live == 0, f"{live} connection task(s) still blocked in drain() 5 s after a 1 s idle timeout"
    finally:
        s.close()
        srv.stop()


REPO = str(Path(v2fhir.__file__).resolve().parents[1])

SERVE = textwrap.dedent(f"""
    import asyncio, resource, sys
    sys.dont_write_bytecode = True
    sys.path.insert(0, {REPO!r})
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
    from v2fhir.mllp import MLLPServer
    server = MLLPServer(lambda fr: b"MSH|^~\\\\&|V2FHIR|BRIDGE\\rMSA|AA|OK1\\r", "127.0.0.1", 0)   # default idle_timeout 300 s
    async def main():
        await server.start()
        print(server.port, flush=True)
        await server.serve_forever()
    asyncio.run(main())
""")


@pytest.mark.skipif(sys.platform == "win32", reason="uses resource.setrlimit")
def test_seventy_silent_connections_do_not_lock_out_a_real_sender():
    """With a 64-fd limit, 70 peers that connect and send nothing used to leave the listener unable to accept (EMFILE)."""
    p = subprocess.Popen([sys.executable, "-c", SERVE], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    silent = []
    try:
        port = int(p.stdout.readline())
        for _ in range(70):                                   # connect, send nothing, keep the socket open
            silent.append(socket.create_connection(("127.0.0.1", port), timeout=5))
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sender:
            sender.sendall(b"\x0bMSH|^~\\&|RIS|H|V2FHIR|B|2026||ADT^A04|OK1|P|2.5\r\x1c\r")
            try:
                ack = sender.recv(4096)
            except TimeoutError:
                pytest.fail("real sender got no ACK within 5 s: the listener stopped accepting (EMFILE) "
                            "while 70 silent peers held their connections")
        assert b"MSA|AA|OK1" in ack
    finally:
        for s in silent:
            s.close()
        p.kill()
        p.wait()


# ---- a hung FHIR server: the sender still gets its ACK before it gives up -----------------------------------
def _hung_fhir_server() -> socket.socket:
    hung = socket.socket()                      # completes the TCP handshake, never answers
    hung.bind(("127.0.0.1", 0))
    hung.listen(16)
    return hung


def test_ack_arrives_within_the_ack_deadline_when_the_fhir_server_hangs(cfg):
    """With 15 s timeouts and 2 retries a hung server used to take 46.5 s, past a typical 30 s sender timeout."""
    hung = _hung_fhir_server()
    cfg.fhir_base_url, cfg.out_dir = f"http://127.0.0.1:{hung.getsockname()[1]}/fhir", ""
    cfg.fhir_timeout_seconds, cfg.fhir_retries, cfg.ack_deadline_seconds = 10, 2, 2.0
    bridge = Bridge(cfg)
    srv = ServerThread(MLLPServer(bridge_handler(bridge), "127.0.0.1", 0)).start()
    try:
        with MLLPClient("127.0.0.1", srv.port, timeout=10) as c:
            t0 = time.monotonic()
            ack = c.send(r.adt("A04", "HUNG1"))
            assert b"MSA|AR|HUNG1" in ack and time.monotonic() - t0 < 4.0
    finally:
        srv.stop()
        bridge.close()
        hung.close()


def test_a_second_connection_waiting_for_the_fhir_lock_still_gets_its_ack_in_time(cfg):
    """FHIR writes are serialised across connections on purpose; the wait for the lock counts against the deadline."""
    hung = _hung_fhir_server()
    cfg.fhir_base_url, cfg.out_dir = f"http://127.0.0.1:{hung.getsockname()[1]}/fhir", ""
    cfg.fhir_timeout_seconds, cfg.fhir_retries, cfg.ack_deadline_seconds = 10, 0, 2.0
    bridge = Bridge(cfg)
    srv = ServerThread(MLLPServer(bridge_handler(bridge), "127.0.0.1", 0)).start()
    try:
        first = MLLPClient("127.0.0.1", srv.port, timeout=10)
        first.send_raw(frame(r.adt("A04", "SLOW1").encode()))            # holds the lock until its deadline
        time.sleep(0.3)
        with MLLPClient("127.0.0.1", srv.port, timeout=10) as second:
            t0 = time.monotonic()
            ack = second.send(r.adt("A04", "WAIT1"))
            assert b"MSA|AR|WAIT1" in ack and time.monotonic() - t0 < 3.0
        first.close()
    finally:
        srv.stop()
        bridge.close()
        hung.close()


def test_shipped_worst_case_fits_inside_the_bundled_client_timeout(cfg):
    assert cfg.ack_deadline_seconds < 30          # MLLPClient's default timeout, and a common engine default


EVICT_MSG = b"MSH|^~\\&|RIS|H|V2FHIR|B|2026||ADT^A04|OK1|P|2.5\r"


def _evict_handler(fr):
    return b"MSH|^~\\&|V2FHIR|B|RIS|H|2026||ACK|X|P|2.5\rMSA|AA|OK1\r"


def test_a_connection_that_is_receiving_a_frame_is_not_evicted_before_a_silent_one():
    """At the connection limit the quietest idle peer is dropped. Activity used to count only whole ACKed frames,
    so a sender part-way through its first message looked quieter than a peer that never sent a byte."""
    srv = ServerThread(MLLPServer(_evict_handler, "127.0.0.1", 0, idle_timeout=30, max_connections=2)).start()
    try:
        streaming = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        streaming.sendall(b"\x0b" + EVICT_MSG)                       # frame started
        time.sleep(0.2)
        silent = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        time.sleep(0.2)
        streaming.sendall(b"OBX|1|TX|x||" + b"y" * 1000)      # still actively sending
        time.sleep(0.2)
        newcomer = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        time.sleep(0.3)
        streaming.sendall(b"\r\x1c\r")                       # finish the frame
        try:
            ack = streaming.recv(4096)
        except (ConnectionError, TimeoutError, OSError) as e:
            ack = repr(e).encode()
        assert b"MSA|AA" in ack, f"actively-sending connection was evicted instead of the silent one: {ack!r}"
        silent.close()
        newcomer.close()
        streaming.close()
    finally:
        srv.stop()
