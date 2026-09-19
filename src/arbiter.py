"""One talker on the link at a time, and cheap waiting while idle.

Two scarce things need arbitrating, and they are the same thing seen twice:

  The dongle keeps ONE server session. Pointing it at a second listener
  silently drops the first, so a resident tray and a foreground `ctl.py` run
  are in direct competition.

  Behind the dongle is a single RS-485 master. Two commands in flight means
  two replies interleaved on one wire, and neither can be matched to its
  request with any confidence.

So the rule is settled, and it is the same one RouterOps uses for the router's
single admin session: **the foreground task wins, the resident readout
yields.** `ctl.py` and `collector.py` claim the link for their whole run. The
tray claims it with a zero timeout before each sample, and if it cannot, it
drops its session and shows "paused" rather than a stale number -- and does
not reconnect until the mutex is free again.

A named mutex rather than a flag or a lock file, for the reason RouterOps
gives: Windows releases it when the owning process dies, however it dies, so
there is no state that can wedge the tool shut.

On idle cost: nothing here spins. `Idle.wait()` is `threading.Event.wait`,
which on Windows bottoms out in a kernel wait on a semaphore -- the thread is
descheduled until the timeout expires or someone signals it. A
`while True: sleep(1)` loop is a worse version of what the OS already
provides, which is why there isn't one anywhere in this project.
"""
from __future__ import annotations

import contextlib
import sys
import threading

LINK_MUTEX = "PhantomBridge.Link"

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                       wintypes.LPCWSTR]
    _kernel32.ReleaseMutex.restype = wintypes.BOOL
    _kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    _WAIT_OBJECT_0 = 0x0
    _WAIT_ABANDONED = 0x80          # previous owner died holding it -- it is ours


class LinkClaim:
    """Exclusive use of the dongle link, process-wide.

    Outside Windows this degrades to an in-process lock, which is enough for
    development but does not arbitrate between separate processes.
    """

    _fallback = threading.Lock()

    def __init__(self, name: str = LINK_MUTEX):
        self.name = name
        self._handle = None
        self._held = False

    def try_acquire(self, timeout_ms: int = 0) -> bool:
        if self._held:
            return True
        if not _IS_WINDOWS:
            self._held = LinkClaim._fallback.acquire(blocking=timeout_ms != 0,
                                                     timeout=max(timeout_ms, 0) / 1000
                                                     if timeout_ms else -1)
            return self._held
        if self._handle is None:
            self._handle = _kernel32.CreateMutexW(None, False,
                                                  "Local\\" + self.name)
            if not self._handle:
                return False
        result = _kernel32.WaitForSingleObject(self._handle, timeout_ms)
        self._held = result in (_WAIT_OBJECT_0, _WAIT_ABANDONED)
        return self._held

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        if not _IS_WINDOWS:
            with contextlib.suppress(RuntimeError):
                LinkClaim._fallback.release()
            return
        _kernel32.ReleaseMutex(self._handle)

    def close(self) -> None:
        self.release()
        if _IS_WINDOWS and self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "LinkClaim":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def held(self) -> bool:
        return self._held


@contextlib.contextmanager
def claim_link(timeout_ms: int = 0, name: str = LINK_MUTEX):
    """Context manager yielding True if we got the link, False if someone has it."""
    claim = LinkClaim(name)
    try:
        yield claim.try_acquire(timeout_ms)
    finally:
        claim.close()


class Idle:
    """A stoppable, interruptible wait. Blocks in the kernel; never spins."""

    def __init__(self):
        self._stop = threading.Event()
        self._wake = threading.Event()

    def wait(self, seconds: float) -> bool:
        """Sleep for `seconds`. Returns False once stopped.

        Returns early if someone calls wake() -- that is how a "refresh now"
        menu click gets a fresh sample immediately without shortening the
        normal interval.
        """
        if self._stop.is_set():
            return False
        self._wake.wait(seconds)
        self._wake.clear()
        return not self._stop.is_set()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def block_until_stopped(self) -> None:
        """Park the main thread with no cost until stop() is called."""
        self._stop.wait()
