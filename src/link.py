"""Transport to the inverter. Three ways in, one request/response API.

  DirectLink   We open TCP to the dongle. If the dongle exposes a local
               passthrough port this is the best path by far: nothing is
               reconfigured, so WatchPower keeps working and there is nothing
               to restore. probe.py reports whether it is available.

  ServerLink   We listen, and `set>server=...;` on UDP 58899 tells the dongle
               to connect to us instead of the vendor cloud. Works even when
               the dongle has no listening port, at the cost of silencing the
               app until --restore.

  DryRunLink   Builds and prints frames, sends nothing. Everything downstream
               of framing can be exercised without hardware.

The session below answers FC=1 heartbeats with site-local time (goal #1's
cheapest path), auto-detects the Eybond dialect the moment a CRC-valid PI30
reply arrives, and serialises requests so a reply can be matched to its
command even if the dongle does not echo our transaction id.
"""
from __future__ import annotations

import datetime as dt
import queue
import socket
import threading
import time

import frames
from frames import FC_FORWARD, FC_HEARTBEAT, Reply


class LinkError(RuntimeError):
    pass


def udp_config(command: str, target: str | None, port: int,
               broadcasts, timeout: float = 3.0, log=print) -> list[tuple[str, bytes]]:
    """Send one `set>...;` string to the dongle. This MUTATES dongle config.

    With no known IP we fan out over every plausible broadcast address rather
    than trusting 255.255.255.255 to leave by the right interface.
    """
    targets = [target] if target else list(broadcasts)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    replies: list[tuple[str, bytes]] = []
    try:
        for dest in targets:
            try:
                sock.sendto(command.encode(), (dest, port))
                log(f"  -> {dest}:{port}  {command}")
            except OSError as exc:
                log(f"  -> {dest}:{port}  send failed: {exc}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                sock.settimeout(max(0.1, deadline - time.time()))
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                break
            except OSError:
                break
            replies.append((addr[0], data))
            log(f"  <- {addr[0]}  {data!r}")
    finally:
        sock.close()
    if not replies:
        log("  (no reply -- it may still have applied; confirm by connection)")
    return replies


class Session:
    """One connected dongle. Thread-safe request/response over the TCP link."""

    def __init__(self, sock: socket.socket, peer: str, *,
                 dialect: str = frames.DEFAULT_DIALECT,
                 auto_dialect: bool = True,
                 tzinfo: dt.tzinfo | None = None,
                 heartbeat_format: str = "bin",
                 devcode: int = frames.DEVCODE_VOLTRONIC,
                 devaddr: int = frames.DEVADDR_DEFAULT,
                 log=None):
        self.sock = sock
        self.peer = peer
        self.dialect = dialect
        self.auto_dialect = auto_dialect
        self.dialect_confirmed = False
        self.tzinfo = tzinfo or dt.timezone.utc
        self.heartbeat_format = heartbeat_format
        self.devcode = devcode
        self.devaddr = devaddr
        self.log = log or (lambda record: None)

        self.heartbeats = 0
        self.last_seen = time.time()
        self.closed = threading.Event()

        self._buffer = b""
        self._tid = 0
        self._tid_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._inbox: queue.Queue = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)

    # -- plumbing ---------------------------------------------------------

    def start(self) -> None:
        self._reader.start()

    def close(self) -> None:
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def _next_tid(self) -> int:
        with self._tid_lock:
            self._tid = (self._tid % 65534) + 1
            return self._tid

    def _read_loop(self) -> None:
        self.sock.settimeout(1.0)
        while not self.closed.is_set():
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            self.last_seen = time.time()
            self._buffer += chunk
            self._drain()
        self.closed.set()
        self.log({"event": "disconnect", "peer": self.peer})

    def _drain(self) -> None:
        while self._buffer:
            if self.auto_dialect and not self.dialect_confirmed:
                self._try_confirm_dialect()
            taken = frames.take_frame(self._buffer, self.dialect)
            if taken is None:
                if len(self._buffer) > 8192:
                    # Nothing parses and the buffer is running away. Keep the
                    # evidence, resync on the next '(' and carry on.
                    self.log({"event": "resync", "dropped": self._buffer[:64].hex()})
                    marker = self._buffer.find(b"(", 1)
                    self._buffer = self._buffer[marker:] if marker > 0 else b""
                return
            header, payload, consumed = taken
            self._buffer = self._buffer[consumed:]
            self._dispatch(header, payload)

    def _try_confirm_dialect(self) -> None:
        ranked = frames.detect_dialects(self._buffer)
        if not ranked:
            return
        name, why = ranked[0]
        if name != self.dialect:
            self.log({"event": "dialect", "from": self.dialect, "to": name,
                      "evidence": why})
            self.dialect = name
        else:
            self.log({"event": "dialect", "confirmed": name, "evidence": why})
        self.dialect_confirmed = True

    def _dispatch(self, header, payload: bytes) -> None:
        tid, devcode, _wire_len, devaddr, fc = header
        if fc == FC_HEARTBEAT:
            self.heartbeats += 1
            self._send_heartbeat_reply(tid)
            self.log({"event": "heartbeat", "n": self.heartbeats,
                      "payload": payload.hex()})
            return

        reply = frames.parse_pi30_reply(payload)
        if reply is None:
            self.log({"event": "unparsed", "tid": tid, "fc": fc,
                      "devcode": hex(devcode), "addr": devaddr,
                      "raw": payload.hex(),
                      "ascii": payload.decode("ascii", "replace").strip()})
            return
        if not reply.crc_ok:
            self.log({"event": "crc-warning", "tid": tid, "text": reply.text})
        self._inbox.put((tid, reply))

    def _send_heartbeat_reply(self, tid: int) -> None:
        """Hand the collector the current site-local time.

        The original spike sent UTC here, which on a UTC+5 site would have set
        the clock five hours slow if the firmware does act on it.
        """
        from clock import heartbeat_time_payload
        now = dt.datetime.now(self.tzinfo)
        payload = heartbeat_time_payload(now, self.heartbeat_format)
        self._send(frames.build(payload, tid=tid, fc=FC_HEARTBEAT,
                                dialect=self.dialect, devcode=self.devcode,
                                devaddr=self.devaddr))

    def _send(self, data: bytes) -> None:
        with self._send_lock:
            self.sock.sendall(data)

    # -- the API ----------------------------------------------------------

    def request(self, command: str, timeout: float = 6.0) -> Reply:
        """Send a PI30 command and wait for its reply. One at a time."""
        if self.closed.is_set():
            raise LinkError("session is closed")
        with self._request_lock:
            while True:                      # discard anything stale
                try:
                    self._inbox.get_nowait()
                except queue.Empty:
                    break
            tid = self._next_tid()
            frame = frames.build(frames.pi30(command), tid=tid, fc=FC_FORWARD,
                                 dialect=self.dialect, devcode=self.devcode,
                                 devaddr=self.devaddr)
            self.log({"event": "tx", "cmd": command, "tid": tid,
                      "dialect": self.dialect, "raw": frame.hex()})
            self._send(frame)

            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise LinkError(f"{command}: no reply within {timeout:.0f}s")
                try:
                    got_tid, reply = self._inbox.get(timeout=remaining)
                except queue.Empty:
                    raise LinkError(f"{command}: no reply within {timeout:.0f}s")
                # Only one command is ever outstanding and the inbox was
                # drained before sending, so this reply is ours. The tid is
                # logged rather than enforced -- whether the dongle echoes it
                # is one of the things we are here to find out.
                if got_tid != tid:
                    self.log({"event": "tid-mismatch", "sent": tid,
                              "got": got_tid, "cmd": command})
                return reply


class BaseLink:
    session: Session | None = None

    def request(self, command: str, timeout: float = 6.0) -> Reply:
        if self.session is None or self.session.closed.is_set():
            raise LinkError("no live session with the dongle")
        return self.session.request(command, timeout)

    def close(self) -> None:
        if self.session is not None:
            self.session.close()


class DirectLink(BaseLink):
    """We connect out to the dongle. Leaves the vendor cloud untouched."""

    def __init__(self, host: str, port: int, **session_kwargs):
        self.host = host
        self.port = port
        self.session_kwargs = session_kwargs

    def connect(self, timeout: float = 5.0) -> Session:
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.session = Session(sock, self.host, **self.session_kwargs)
        self.session.start()
        return self.session


class ServerLink(BaseLink):
    """The dongle connects to us, after a `set>server=` redirect."""

    def __init__(self, bind_host: str, port: int, **session_kwargs):
        self.bind_host = bind_host
        self.port = port
        self.session_kwargs = session_kwargs
        self._server: socket.socket | None = None
        self._arrived = threading.Event()
        self._stop = threading.Event()

    def start(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.bind_host, self.port))
        self._server.listen(4)
        self._server.settimeout(1.0)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        log = self.session_kwargs.get("log") or (lambda record: None)
        while not self._stop.is_set():
            try:
                sock, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            log({"event": "connect", "peer": addr[0], "port": addr[1]})
            if self.session is not None and not self.session.closed.is_set():
                self.session.close()
            self.session = Session(sock, addr[0], **self.session_kwargs)
            self.session.start()
            self._arrived.set()

    def wait_for_session(self, timeout: float = 120.0) -> Session | None:
        if self._arrived.wait(timeout):
            return self.session
        return None

    def close(self) -> None:
        self._stop.set()
        super().close()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass


class DryRunLink(BaseLink):
    """Builds frames, sends nothing. For exercising the stack without hardware."""

    def __init__(self, dialect: str = frames.DEFAULT_DIALECT, log=print):
        self.dialect = dialect
        self.log = log
        self._tid = 0

    def request(self, command: str, timeout: float = 6.0) -> Reply:
        self._tid += 1
        frame = frames.build(frames.pi30(command), tid=self._tid,
                             dialect=self.dialect)
        self.log(f"  would send {command:<18} {frame.hex()}")
        raise LinkError("dry run -- nothing was sent")

    def close(self) -> None:
        pass
