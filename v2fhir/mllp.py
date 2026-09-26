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
No exception from a message or a connection can stop the listener.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass

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
                 idle_timeout: float = 300.0):
        self.handler = handler
        self.host, self.port = host, port
        self.max_message_bytes = max_message_bytes
        self.idle_timeout = idle_timeout
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closing = False
        self.connections = 0
        self.messages = 0

    async def start(self) -> MLLPServer:
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
        t = asyncio.get_running_loop().create_task(self._client(reader, writer))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
                    try:
                        ack = await asyncio.to_thread(self.handler, fr)
                    except Exception:  # noqa: BLE001 -- handler bug: log it, keep the connection and the listener
                        log.exception("MLLP handler failed")
                        ack = None
                    if ack:
                        writer.write(frame(ack))
                        await writer.drain()
        except (ConnectionError, OSError) as e:
            log.info("connection %s dropped: %s", peer, e)
        except Exception:  # noqa: BLE001
            log.exception("unexpected error on connection %s", peer)
        finally:
            if decoder.discarded:
                log.warning("%s: %d bytes outside MLLP frames discarded", peer, decoder.discarded)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


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
