"""Write icon.ico: the icon the frozen exe wears.

    python icon.py            writes icon.ico next to this file

It is the tray's own drawing (tray.render_icon) at a full green 100, so
the file in Explorer, the Task Manager row and the glyph in the
notification area are one shape. Sizes 16, 24, 32 and 48 are stored as
bitmaps, the way every Windows icon is; 256 is a PNG entry, the only form
Vista+ accepts at that size. No GDI, no Pillow: the tray already draws in
a plain BGRA buffer, and an .ico is just a header over those bytes.

icon.ico is committed so a clone builds; run this again after changing the
drawing.
"""
from __future__ import annotations

import pathlib
import struct
import sys
import zlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

import tray

SIZES = (16, 24, 32, 48, 256)
BMP_MAX = 48


def _png(size: int, px: bytearray) -> bytes:
    raw = bytearray()
    for y in range(size):
        raw.append(0)                                # filter: none
        row = px[y * size * 4:(y + 1) * size * 4]
        for x in range(size):
            b, g, r, a = row[x * 4:x * 4 + 4]
            raw += bytes((r, g, b, a))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def _bmp(size: int, px: bytearray) -> bytes:
    """A 32-bpp icon bitmap: BITMAPINFOHEADER with the height doubled for
    the AND mask, rows bottom-up, then the mask (all zero: alpha does the
    masking, but the mask must be there for the entry to be well-formed)."""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    rows = b"".join(bytes(px[y * size * 4:(y + 1) * size * 4])
                    for y in reversed(range(size)))
    mask = bytes(((size + 31) // 32) * 4) * size
    return header + rows + mask


def build(path: pathlib.Path) -> None:
    images = []
    for size in SIZES:
        px = tray.render_icon(tray.GREEN, 1.0, 100, size)
        images.append((size, _png(size, px) if size > BMP_MAX else _bmp(size, px)))

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = [], []
    for size, data in images:
        dim = 0 if size >= 256 else size
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                                   len(data), offset))
        blobs.append(data)
        offset += len(data)
    path.write_bytes(header + b"".join(entries) + b"".join(blobs))


if __name__ == "__main__":
    out = HERE / "icon.ico"
    build(out)
    print(f"wrote {out} ({out.stat().st_size} bytes, sizes {SIZES})")
