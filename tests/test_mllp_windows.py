"""Windows: asyncio's default Proactor loop closes the listening socket for good when an accept fails with
WinError 64 (the client reset during accept: a timed-out sender, a load-balancer probe). The listener uses the
selector loop instead. This fakes the Proactor's failing accept, so the Windows behaviour runs on any OS."""
import asyncio
import logging
import signal
import socket
import threading
import time
from asyncio import proactor_events

import pytest

from v2fhir.mllp import MLLPServer, ServerThread


class FakeIocpProactor:
    """Stands in for asyncio.windows_events.IocpProactor. The first accept fails with WinError 64."""

    def __init__(self):
        self.accepts = 0
        self.loop = None

    def set_loop(self, loop):
        self.loop = loop

    def recv(self, sock, nbytes, flags=0):          # the loop's self-pipe read; never completes
        return self.loop.create_future()

    def accept(self, listener):
        self.accepts += 1
        fut = self.loop.create_future()
        if self.accepts == 1:                        # a sender that timed out / a probe that sent RST
            fut.set_exception(OSError(22, "The specified network name is no longer available", None, 64))
        return fut                                   # later accepts stay pending (kernel still queues connects)

    def select(self, timeout=None):
        time.sleep(0.005 if timeout is None else min(timeout, 0.005))
        return []

    def _stop_serving(self, obj):
        pass

    def close(self):
        pass


class WindowsLikePolicy(asyncio.DefaultEventLoopPolicy):
    def new_event_loop(self):
        return proactor_events.BaseProactorEventLoop(FakeIocpProactor())


@pytest.fixture
def windows_default_loop():
    old = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(WindowsLikePolicy())
    yield
    asyncio.set_event_loop_policy(old)
    if threading.current_thread() is threading.main_thread():
        signal.set_wakeup_fd(-1)


def test_listener_keeps_accepting_after_a_client_aborts_during_accept(windows_default_loop, caplog):
    caplog.set_level(logging.ERROR)
    srv = ServerThread(MLLPServer(lambda fr: None, "127.0.0.1", 0)).start()
    try:
        time.sleep(0.3)                                             # let the accept loop run once
        port = srv.port
        with socket.create_connection(("127.0.0.1", port), timeout=2):   # next sender must still get in
            pass
    except ConnectionRefusedError:
        pytest.fail("MLLP listener closed its listening socket after one aborted accept; "
                    f"log: {[r.getMessage() for r in caplog.records]}")
    finally:
        srv.stop()



def test_connection_limit_stays_under_the_windows_select_limit(monkeypatch):
    """The selector loop on Windows uses select(), which handles at most 512 sockets."""
    from v2fhir import mllp
    monkeypatch.setattr(mllp.sys, "platform", "win32")
    assert mllp._fd_budget() <= 500
