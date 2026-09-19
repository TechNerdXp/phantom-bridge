"""Voltronic PI30 <-> Eybond/Shinemonitor dongle framing.

Two layers live here, and they have very different confidence levels:

  PI30 (inner)    PROVEN. `pi30("QPIGS")` is byte-identical to the published
                  frame. CRC16-XMODEM and the 0x28/0x0D/0x0A escape rule are
                  correct. Doctested below.

  Eybond (outer)  HYPOTHESIS. Reconstructed from public reverse-engineering of
                  the Eybond Wi-Fi Plug / Shinemonitor collector. The header is
                  8 bytes, but the length field's meaning is guesswork, so we
                  carry several candidate DIALECTS and let the hardware pick.

`detect_dialects()` is how the guess gets settled: feed it the first bytes the
dongle actually sends and it ranks the dialects that explain the stream. A
dialect only scores if the payload it carves out is a CRC-valid PI30 response,
which is a strong enough signal to trust.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

DEVCODE_VOLTRONIC = 0x0993   # Axpert / Voltronic family
DEVADDR_DEFAULT = 0x01       # inverter address on the RS-485 bus

FC_HEARTBEAT = 0x01          # dongle <-> server keepalive (server sends time)
FC_FORWARD = 0x04            # server -> dongle -> inverter, raw payload

HEADER_SIZE = 8

# bytes the Voltronic parser would otherwise mistake for framing
ESCAPE_BYTES = (0x28, 0x0D, 0x0A)


# --------------------------------------------------------------------------
# PI30 inner layer -- proven
# --------------------------------------------------------------------------

def crc16_xmodem(data: bytes) -> int:
    """CRC-16/XMODEM: poly 0x1021, init 0x0000, no reflection, no final xor."""
    crc = 0x0000
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def escape(byte: int) -> int:
    """Voltronic quirk: a CRC byte colliding with '(', CR or LF is incremented."""
    return byte + 1 if byte in ESCAPE_BYTES else byte


def pi30_crc(body: bytes) -> bytes:
    """The two escaped CRC bytes that follow `body` on the wire."""
    crc = crc16_xmodem(body)
    return bytes((escape((crc >> 8) & 0xFF), escape(crc & 0xFF)))


def pi30(command: str) -> bytes:
    """Wrap an ASCII PI30 command: <cmd><crc_hi><crc_lo><CR>.

    >>> pi30("QPIGS").hex()
    '5150494753b7a90d'
    """
    body = command.encode("ascii")
    return body + pi30_crc(body) + b"\r"


@dataclass(frozen=True)
class Reply:
    """A decoded PI30 response payload."""
    text: str            # inside the parens, e.g. "230.0 50.0 ..." or "ACK"
    raw: bytes
    crc_ok: bool

    @property
    def is_ack(self) -> bool:
        return self.text.strip().upper() == "ACK"

    @property
    def is_nak(self) -> bool:
        return self.text.strip().upper() == "NAK"


def parse_pi30_reply(payload: bytes) -> Reply | None:
    """Decode `(DATA<crc_hi><crc_lo><CR>` into a Reply, or None if unrecognisable.

    The CRC is checked but a mismatch is reported, not fatal -- some firmwares
    are sloppy on the return path and we would rather see the data and the
    warning than throw it away.

    >>> r = parse_pi30_reply(b"(ACK" + pi30_crc(b"(ACK") + b"\\r")
    >>> r.is_ack, r.crc_ok
    (True, True)
    """
    buf = payload.rstrip(b"\x00")
    if not buf.startswith(b"("):
        return None
    if buf.endswith(b"\r"):
        buf = buf[:-1]
    if len(buf) < 3:
        return None
    body, crc_rx = buf[:-2], buf[-2:]
    crc_ok = pi30_crc(body) == crc_rx
    if not crc_ok:
        # Maybe the CRC/CR were already stripped upstream -- try the whole thing.
        alt = buf
        if pi30_crc(alt[:-2]) != alt[-2:]:
            body = buf
    text = body[1:].decode("ascii", "replace").strip()
    return Reply(text=text, raw=payload, crc_ok=crc_ok)


# --------------------------------------------------------------------------
# Eybond outer layer -- hypothesis, hence dialects
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Dialect:
    """One candidate reading of the 8-byte header.

    `len_mode` says what the 2-byte length field counts:
      "tail"   wire_len = len(payload) + 2   (devaddr + fc + payload)
      "body"   wire_len = len(payload)
      "frame"  wire_len = len(payload) + 8   (whole frame including header)
    """
    name: str
    len_mode: str
    note: str = ""

    def wire_len(self, payload: bytes) -> int:
        return len(payload) + {"tail": 2, "body": 0, "frame": HEADER_SIZE}[self.len_mode]

    def payload_len(self, wire_len: int) -> int:
        return wire_len - {"tail": 2, "body": 0, "frame": HEADER_SIZE}[self.len_mode]


DIALECTS: dict[str, Dialect] = {
    "tail": Dialect("tail", "tail", "len = payload + 2 (devaddr+fc). Current best guess."),
    "body": Dialect("body", "body", "len = payload only."),
    "frame": Dialect("frame", "frame", "len = whole frame including the 8-byte header."),
    "raw": Dialect("raw", "body", "No Eybond header at all -- transparent RS-485."),
}

DEFAULT_DIALECT = "tail"


def build(payload: bytes, *, tid: int, fc: int = FC_FORWARD,
          dialect: str = DEFAULT_DIALECT,
          devcode: int = DEVCODE_VOLTRONIC,
          devaddr: int = DEVADDR_DEFAULT) -> bytes:
    """Frame a payload for the wire under the named dialect.

    >>> build(pi30("QPIGS"), tid=1).hex()
    '00010993000a01045150494753b7a90d'
    >>> build(pi30("QPIGS"), tid=1, dialect="raw").hex()
    '5150494753b7a90d'
    """
    if dialect == "raw":
        return payload
    spec = DIALECTS[dialect]
    return struct.pack(">HHHBB", tid & 0xFFFF, devcode, spec.wire_len(payload),
                       devaddr, fc) + payload


def take_frame(buf: bytes, dialect: str = DEFAULT_DIALECT):
    """Carve one frame off the front of `buf`.

    Returns (header_tuple, payload, consumed) or None if more bytes are needed.
    header_tuple is (tid, devcode, wire_len, devaddr, fc); under the "raw"
    dialect it is (0, 0, len, 0, FC_FORWARD) so callers need no special case.
    """
    if dialect == "raw":
        end = buf.find(b"\r")
        if end < 0:
            return None
        payload = buf[:end + 1]
        return (0, 0, len(payload), 0, FC_FORWARD), payload, len(payload)

    if len(buf) < HEADER_SIZE:
        return None
    tid, devcode, wire_len, devaddr, fc = struct.unpack(">HHHBB", buf[:HEADER_SIZE])
    body_len = DIALECTS[dialect].payload_len(wire_len)
    if body_len < 0 or body_len > 4096:
        return None
    total = HEADER_SIZE + body_len
    if len(buf) < total:
        return None
    return (tid, devcode, wire_len, devaddr, fc), buf[HEADER_SIZE:total], total


def detect_dialects(buf: bytes) -> list[tuple[str, str]]:
    """Rank the dialects that explain `buf`, best first.

    Each entry is (dialect_name, evidence). Only meaningful once the dongle has
    actually sent us something -- this is the function that turns the framing
    guess into a fact.
    """
    scored: list[tuple[int, str, str]] = []
    for name in DIALECTS:
        taken = take_frame(buf, name)
        if taken is None:
            continue
        _header, payload, consumed = taken
        reply = parse_pi30_reply(payload)
        if reply is not None and reply.crc_ok:
            score, why = 3, f"payload is a CRC-valid PI30 reply ({consumed} B frame)"
        elif reply is not None:
            score, why = 2, "payload looks like a PI30 reply but the CRC failed"
        elif payload.startswith(b"("):
            score, why = 1, "payload starts with '(' but did not parse"
        else:
            continue
        scored.append((score, name, why))
    scored.sort(reverse=True)
    return [(name, why) for _score, name, why in scored]


# --------------------------------------------------------------------------
# Back-compat shims for the original spike API
# --------------------------------------------------------------------------

def eybond_frame(tid: int, payload: bytes, *, fc: int = FC_FORWARD,
                 devcode: int = DEVCODE_VOLTRONIC,
                 devaddr: int = DEVADDR_DEFAULT) -> bytes:
    """Original name for build() under the default dialect."""
    return build(payload, tid=tid, fc=fc, devcode=devcode, devaddr=devaddr)


def parse_header(buf: bytes):
    """Return (tid, devcode, wire_len, devaddr, fc) from the first 8 bytes."""
    if len(buf) < HEADER_SIZE:
        return None
    return struct.unpack(">HHHBB", buf[:HEADER_SIZE])


def unwrap(buf: bytes):
    """Split a complete frame into (header_tuple, payload). None if short."""
    taken = take_frame(buf)
    if taken is None:
        return None
    header, payload, _consumed = taken
    return header, payload


if __name__ == "__main__":
    import doctest
    print(doctest.testmod())
