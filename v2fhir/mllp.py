"""MLLP (Minimal Lower Layer Protocol, HL7 v2 over TCP).

A frame is   <VT 0x0B> payload <FS 0x1C><CR 0x0D>

TCP is a byte stream, so one read() can hold half a frame, exactly one, several, or the end of one and
the start of the next. FrameDecoder handles all of those, plus the field realities:
  * bytes outside a frame (keep-alives, line noise) are discarded and counted
  * a new VT before the previous frame ended: the partial frame is abandoned
  * a frame larger than max_bytes: only the first `head_bytes` are kept (enough for MSH, so the
    sender still gets a NAK with the right MSA-2) and memory stays bounded
Search restarts where the last one stopped, so a 50 MB message arriving in 64 KB reads is O(n).

MLLPServer: asyncio listener. One task per connection; messages on a connection are handled strictly in
order (HL7 ordering), each on a worker thread so a slow FHIR server never blocks other connections.
No exception from a message or a connection can stop the listener. A peer that goes quiet (reads or ACK writes
stalled past idle_timeout) is dropped, and at max_connections the quietest idle connection makes room.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger("v2fhir.mllp")

# The selector loop everywhere. It is already the default on Linux and macOS; on Windows the default Proactor
# loop closes the listening socket for good when one client resets during accept (a load-balancer probe, a
# sender that timed out), so one bad connection would stop the listener.
loop_factory = asyncio.SelectorEventLoop

VT, FS, CR = b"\x0b", b"\x1c", b"\x0d"
END = FS + CR


def frame(payload: bytes) -> bytes:
    return VT + payload + END


@dataclass
class Frame:
    data: bytes
    size: int
    oversize: bool = False


class FrameDecoder:
    def __init__(self, max_bytes: int = 10 * 1024 * 1024, head_bytes: int = 4096):
        self.max_bytes = max_bytes
        self.head_bytes = head_bytes
        self.buf = bytearray()
        self.in_frame = False
        self.scan = 0              # where the END search resumes
        self.oversize_head: bytes | None = None
        self.oversize_size = 0
        self.discarded = 0         # bytes thrown away outside frames / abandoned frames
        self.abandoned = 0         # partial frames dropped because a new VT arrived

    def feed(self, data: bytes) -> list[Frame]:
        before = self.abandoned
        out = self._feed(data)
        if self.abandoned > before:
            log.warning("MLLP start block inside an unfinished frame; %d partial frame(s) abandoned", self.abandoned - before)
        return out

    def _feed(self, data: bytes) -> list[Frame]:
        self.buf += data
        out: list[Frame] = []
        while True:
            if not self.in_frame:
                start = self.buf.find(VT)
                if start < 0:
                    self.discarded += len(self.buf)
                    self.buf.clear()
                    return out
                self.discarded += start
                del self.buf[:start + 1]
                self.in_frame, self.scan = True, 0
                continue
            # Look for the next VT first and only search for END before it: each byte is examined a constant
            # number of times, so a run of VT bytes can't make this quadratic.
            restart = self.buf.find(VT, self.scan)
            end = self.buf.find(END, self.scan, restart if restart >= 0 else len(self.buf))
            if end < 0 and restart >= 0:           # new frame started before this one ended
                self.abandoned += 1
                self.discarded += restart + self.oversize_size
                del self.buf[:restart]
                self.in_frame, self.oversize_head, self.oversize_size = False, None, 0
                continue
            if end < 0:
                self.scan = max(0, len(self.buf) - 1)       # FS may be the last byte; CR may come next read
                pending = len(self.buf) - (1 if self.buf.endswith(FS) else 0)     # a trailing FS isn't payload
                if pending + self.oversize_size > self.max_bytes:
                    if self.oversize_head is None:
                        self.oversize_head = bytes(self.buf[:self.head_bytes])
                    keep = self.buf[-1:]                     # might be FS
                    self.oversize_size += len(self.buf) - len(keep)
                    self.buf = bytearray(keep)
                    self.scan = 0
                return out
            payload = bytes(self.buf[:end])
            del self.buf[:end + 2]
            self.in_frame, self.scan = False, 0
            if self.oversize_head is not None or len(payload) > self.max_bytes:
                head = self.oversize_head if self.oversize_head is not None else payload[:self.head_bytes]
                out.append(Frame(head, self.oversize_size + len(payload), oversize=True))
                self.oversize_head, self.oversize_size = None, 0
            else:
                out.append(Frame(payload, len(payload)))


Handler = Callable[[Frame], "bytes | None"]


class MLLPServer:
    def __init__(self, handler: Handler, host: str = "127.0.0.1", port: int = 2575, max_message_bytes: int = 10 * 1024 * 1024,
                 idle_timeout: float = 300.0, max_connections: int = 128):
        self.handler = handler
        self.host, self.port = host, port
        self.max_message_bytes = max_message_bytes
        self.idle_timeout = idle_timeout
        self.max_connections = max_connections
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._conns: dict[asyncio.Task, _Conn] = {}
        self._closing = False
        self.connections = 0
        self.messages = 0

    async def start(self) -> MLLPServer:
        self.max_connections = min(self.max_connections, _fd_budget())
        self._server = await asyncio.start_server(self._on_connect, self.host, self.port, limit=2 ** 20)
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("MLLP listening on %s:%d", self.host, self.port)
        return self

    async def serve_forever(self) -> None:
        """Serve until cancelled (Ctrl+C). asyncio.Server.serve_forever() is not used: on Python 3.12+ its
        cancellation waits for every open connection to close, and a sender's connection never does. The
        one-second sleep also lets Ctrl+C through on the Windows selector loop, whose select() isn't interrupted."""
        if self._server is None:
            await self.start()
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            await self.close()

    async def close(self) -> None:
        """Stop accepting, cancel open connections, wait for them. Connections are cancelled first because
        Python 3.12+ wait_closed() waits for every one of them."""
        self._closing = True
        if self._server is not None:
            self._server.close()
        for _ in range(10):                       # a connection accepted during the gather is caught next round
            if not self._tasks:
                break
            for t in list(self._tasks):
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            await asyncio.sleep(0)
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)

    def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Register the connection's task synchronously, so close() can never miss one that was just accepted."""
        if self._closing:
            writer.close()
            return
        if len(self._tasks) >= self.max_connections and not self._evict_one(writer.get_extra_info("peername")):
            log.warning("connection limit (%d) reached and every connection is busy; refusing %s",
                        self.max_connections, writer.get_extra_info("peername"))
            writer.close()
            return
        conn = _Conn(writer)
        t = asyncio.get_running_loop().create_task(self._client(reader, writer, conn))
        self._tasks.add(t)
        self._conns[t] = conn
        t.add_done_callback(self._forget)

    def _forget(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        self._conns.pop(t, None)

    def _evict_one(self, newcomer) -> bool:
        """At the connection limit, drop the connection that has been quiet longest and isn't handling a message
        (preferring ones that never sent a frame), so silent peers can't lock real senders out."""
        idle = [(c.frames > 0, c.last, t) for t, c in self._conns.items() if not c.busy and not c.dropped]
        if not idle:
            return False
        victim = min(idle, key=lambda x: (x[0], x[1]))[2]
        log.warning("connection limit (%d) reached; dropping the quietest connection to admit %s", self.max_connections, newcomer)
        self._conns[victim].drop()
        victim.cancel()
        return True

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, conn: _Conn | None = None) -> None:
        conn = conn or _Conn(writer)
        peer = writer.get_extra_info("peername")
        self.connections += 1
        decoder = FrameDecoder(self.max_message_bytes)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(65536), self.idle_timeout)
                except TimeoutError:
                    log.info("closing idle connection %s", peer)
                    break
                if not data:
                    break
                for fr in decoder.feed(data):
                    self.messages += 1
                    conn.busy = True
                    try:
                        ack = await asyncio.to_thread(self.handler, fr)
                    except Exception:  # noqa: BLE001 -- handler bug: log it, keep the connection and the listener
                        log.exception("MLLP handler failed")
                        ack = None
                    finally:
                        conn.busy = False
                    if ack:
                        writer.write(frame(ack))
                        try:                              # a peer that stops reading ACKs mustn't hold this forever
                            await asyncio.wait_for(writer.drain(), self.idle_timeout)
                        except TimeoutError:
                            log.warning("closing %s: it stopped reading ACKs", peer)
                            conn.drop()                   # close() would wait for the unsent ACK to flush
                            return
                    conn.frames += 1
                    conn.last = time.monotonic()
        except (ConnectionError, OSError) as e:
            log.info("connection %s dropped: %s", peer, e)
        except Exception:  # noqa: BLE001
            log.exception("unexpected error on connection %s", peer)
        finally:
            if decoder.discarded:
                log.warning("%s: %d bytes outside MLLP frames discarded", peer, decoder.discarded)
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 1)


@dataclass
class _Conn:
    writer: asyncio.StreamWriter
    last: float = field(default_factory=time.monotonic)      # connect time, then time of the last ACKed frame
    frames: int = 0
    busy: bool = False                                       # a message is being handled
    dropped: bool = False

    def drop(self) -> None:
        """Close the socket now (its file descriptor is free at once), without waiting for unsent data."""
        self.dropped = True
        self.writer.transport.abort()


def _fd_budget() -> int:
    """Half the process's file-descriptor limit (POSIX): the listener must not run out of fds for the FHIR side."""
    try:
        import resource
        soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        return max(8, soft // 2) if soft > 0 else 1 << 30
    except (ImportError, ValueError, OSError):
        return 1 << 30


class MLLPClient:
    """Blocking client: send a message, wait for its ACK. Used by the demo RIS, the CLI and the tests."""

    def __init__(self, host: str, port: int, timeout: float = 30.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.decoder = FrameDecoder()
        self._pending: list[Frame] = []

    def send(self, payload: bytes | str, encoding: str = "utf-8") -> bytes:
        if isinstance(payload, str):
            payload = payload.encode(encoding)
        self.sock.sendall(frame(payload))
        return self.recv()

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv(self) -> bytes:
        while not self._pending:
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("connection closed before ACK")
            self._pending.extend(self.decoder.feed(data))
        return self._pending.pop(0).data

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()

    def __enter__(self) -> MLLPClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def bridge_handler(bridge) -> Handler:
    """Adapt a Bridge to the MLLP handler signature."""
    def handle(fr: Frame) -> bytes | None:
        result = bridge.handle_oversize(fr.data, fr.size) if fr.oversize else bridge.handle(fr.data)
        return result.ack_bytes
    return handle


class ServerThread:
    """Run an MLLPServer on its own event loop in a background thread (demo, tests, embedding)."""

    def __init__(self, server: MLLPServer):
        self.server = server
        self.loop = loop_factory()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mllp")
        self._ready = threading.Event()
        self._error: BaseException | None = None

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.server.start())
        except BaseException as e:  # noqa: BLE001 -- surface bind errors to start()
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        self.loop.run_forever()
        self.loop.run_until_complete(self.server.close())
        leftover = [t for t in asyncio.all_tasks(self.loop) if not t.done()]      # what asyncio.run() would cancel
        for t in leftover:
            t.cancel()
        if leftover:
            self.loop.run_until_complete(asyncio.gather(*leftover, return_exceptions=True))
        self.loop.close()

    def start(self, timeout: float = 10) -> ServerThread:
        self._thread.start()
        self._ready.wait(timeout)
        if self._error:
            raise self._error
        return self

    @property
    def port(self) -> int:
        return self.server.port

    def stop(self) -> None:
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(10)
