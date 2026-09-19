#!/usr/bin/env python3
"""Resident battery readout in the notification area.

    pythonw tray.py                 follow the collector's state file (default)
    pythonw tray.py --direct <ip>   own the link by connecting out to the dongle
    pythonw tray.py --redirect      own the link by redirecting the dongle here

Windows-only by design, and stdlib-only like the rest of this project -- the
icon is drawn into a DIB and handed to Shell_NotifyIcon through ctypes, the
same way RouterOps does it.

-- what it costs -------------------------------------------------------------
Nothing here spins. The UI thread sits in GetMessageW, which parks it in the
kernel until a message arrives. The poll thread waits on an event with a
timeout, which is the same kind of wait. Two blocked threads and a few
milliseconds of work per sample is the entire idle cost.

In the default --follow mode it opens no link at all: it reads the small JSON
state file the collector publishes each cycle. That is zero extra RS-485
traffic, zero extra dongle sessions, and it works no matter which process owns
the link.

-- sharing the link ----------------------------------------------------------
The dongle keeps exactly ONE server session and there is one RS-485 master
behind it, so a resident readout and a foreground `ctl.py` run cannot both
talk. Same rule as RouterOps and for the same reason: the foreground task
wins, the readout yields.

When it owns the link, this claims Local\\PhantomBridge.Link before each
sample. If it cannot, someone else is driving: it drops its own session and
falls back to the state file rather than showing a stale number, and does not
reconnect until the mutex is free again.

-- what the percentage actually means ----------------------------------------
The inverter's SOC is voltage-derived unless closed-loop BMS comms are
confirmed, and on a flat LiFePO4 curve that number is close to meaningless.
So the icon shows it, because it was asked for and it is the only percentage
there is -- but the *colour* is driven by the two things that are solid: work
mode and discharge current. The tooltip always states the basis and carries
the battery voltage, which is the honest number. Set TRUST_SOC in config.py
once BMS comms are confirmed and the caveat goes away.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import pathlib
import sys
import threading
from ctypes import wintypes

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

import arbiter
import bridge
import config

if sys.platform != "win32":
    raise SystemExit("tray.py is Windows-only.")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

LRESULT = ctypes.c_ssize_t

WM_DESTROY, WM_COMMAND, WM_CLOSE = 0x0002, 0x0111, 0x0010
WM_RBUTTONUP, WM_LBUTTONUP = 0x0205, 0x0202
WM_APP_TRAY = 0x8001        # tray icon callbacks land here
WM_APP_SAMPLE = 0x8002      # poll thread -> UI thread: new reading ready

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

MF_STRING, MF_SEPARATOR, MF_GRAYED, MF_CHECKED = 0x0000, 0x0800, 0x0001, 0x0008
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100

IDI_APPLICATION = 32512
SM_CXSMICON = 49

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "PhantomBridgeTray"

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# Every handle-returning and handle-taking call is declared. ctypes defaults an
# undeclared argument to C int, and an HICON or HBITMAP on 64-bit Windows does
# not fit in one -- an undeclared DeleteObject silently truncates the handle
# and frees nothing, which is a GDI leak that takes about a day to turn into a
# visibly broken desktop.
HGDIOBJ = wintypes.HANDLE
HMENU = wintypes.HANDLE

user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HWND, HMENU,
                                   wintypes.HINSTANCE, wintypes.LPVOID]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]

user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.CreateIconIndirect.restype = wintypes.HICON
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.LoadIconW.restype = wintypes.HICON
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]

user32.CreatePopupMenu.restype = HMENU
user32.AppendMenuW.argtypes = [HMENU, wintypes.UINT, ctypes.c_size_t,
                               wintypes.LPCWSTR]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [HMENU, wintypes.UINT, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                  wintypes.LPVOID]
user32.DestroyMenu.argtypes = [HMENU]

gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC,
                                   ctypes.POINTER(BITMAPINFOHEADER),
                                   wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateBitmap.restype = wintypes.HBITMAP
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT,
                               wintypes.UINT, wintypes.LPVOID]
gdi32.DeleteObject.argtypes = [HGDIOBJ]

shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                      ctypes.POINTER(NOTIFYICONDATAW)]

user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.GetMessageW.restype = wintypes.BOOL

kernel32.GetModuleHandleW.restype = wintypes.HMODULE


# -- the glyph ----------------------------------------------------------------

# Colours are driven by mode and current, never by the percentage, because the
# percentage is the part we do not trust.
GREEN = (90, 200, 110)      # charging from grid or PV
BLUE = (100, 170, 235)      # on grid, battery idle
AMBER = (255, 176, 0)       # on battery, comfortable
ORANGE = (255, 120, 40)     # on battery, getting on with it
RED = (235, 60, 60)         # on battery and low, or a fault
GREY = (140, 140, 140)      # no data

SHELL_ALPHA = 200           # battery outline
FILL_ALPHA = 255
EMPTY_ALPHA = 55            # unfilled interior: visible on both taskbar themes


def _icon_size() -> int:
    return user32.GetSystemMetrics(SM_CXSMICON) or 16


# A 3x5 pixel font. At a 16 px icon "73" is 7 px wide and "100" is 11 px, so
# the real percentage fits without abbreviating it -- which is the whole point
# of putting the number on the icon rather than only in the tooltip.
DIGITS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "001", "001"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "-": ("000", "000", "111", "000", "000"),
}
GLYPH_W, GLYPH_H = 3, 5


def _rect(px, size, x0, y0, x1, y1, colour, alpha) -> None:
    """Fill an inclusive-exclusive rect in a top-down BGRA buffer."""
    for y in range(max(0, y0), min(size, y1)):
        row = y * size * 4
        for x in range(max(0, x0), min(size, x1)):
            off = row + x * 4
            px[off:off + 4] = bytes((colour[2], colour[1], colour[0], alpha))


def _draw_text(px, size, text, colour, alpha, scale, top) -> None:
    """Centre a run of 3x5 glyphs horizontally at `top`, scaled by `scale`."""
    gap = scale
    width = len(text) * GLYPH_W * scale + (len(text) - 1) * gap
    left = (size - width) // 2
    for index, char in enumerate(text):
        glyph = DIGITS.get(char)
        if glyph is None:
            continue
        ox = left + index * (GLYPH_W * scale + gap)
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    x = ox + col * scale
                    y = top + row * scale
                    _rect(px, size, x, y, x + scale, y + scale, colour, alpha)


def make_icon(colour, fraction, soc=None, size=None) -> int:
    """The state-of-charge number, plus a proportional bar beneath it.

    The number is the thing being asked for, so it gets the space: digits fill
    the upper two thirds and the bar is a 2 px strip under them. Both derive
    from `size`, so a 20, 24 or 32 px icon on a high-DPI taskbar is drawn at
    its own scale rather than being a 16 px drawing stretched.

    Caller owns the returned HICON and must DestroyIcon it.
    """
    size = size or _icon_size()
    unit = size / 16.0
    px = bytearray(size * size * 4)          # transparent to start

    text = "--" if soc is None else str(int(round(soc)))

    # The bar is measured first so the digits can take whatever is left, which
    # is what keeps the two in proportion at any icon size.
    bar_h = max(2, round(2 * unit))
    bar_top = size - bar_h

    # Largest scale whose glyphs fit the height above the bar and the icon
    # width, keeping at least a pixel of air between the digits and the bar.
    def width_at(sc):
        return len(text) * GLYPH_W * sc + (len(text) - 1) * sc

    scale = 1
    while (GLYPH_H * (scale + 1) <= bar_top - 1
           and width_at(scale + 1) <= size - 2):
        scale += 1

    # Centred in the whole space above the bar. Centring inside a smaller
    # "available" band was what made the glyph sit high with a dead strip
    # under it.
    _draw_text(px, size, text, colour, FILL_ALPHA, scale,
               max(0, (bar_top - GLYPH_H * scale) // 2))

    # Proportional bar: the empty part stays visible so the glyph keeps one
    # shape in every state instead of the bar vanishing at 0%.
    _rect(px, size, 0, bar_top, size, size, colour, EMPTY_ALPHA)
    if fraction is not None:
        filled = max(0, min(size, int(round(size * max(0.0, min(1.0, fraction))))))
        if fraction > 0 and filled == 0:
            filled = 1                       # never render a live pack as empty
        _rect(px, size, 0, bar_top, filled, size, colour, FILL_ALPHA)

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth, bmi.biHeight = size, -size  # negative = top-down
    bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0

    hdc = user32.GetDC(None)
    bits = ctypes.c_void_p()
    colour_bmp = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0,
                                        ctypes.byref(bits), None, 0)
    user32.ReleaseDC(None, hdc)
    if not colour_bmp:
        return user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))
    ctypes.memmove(bits, bytes(px), len(px))

    # Alpha does the masking on a 32-bpp icon, so the mask bitmap only has to
    # exist -- but it does have to be deleted, like the colour one.
    mask_bmp = gdi32.CreateBitmap(size, size, 1, 1, None)
    info = ICONINFO(True, 0, 0, mask_bmp, colour_bmp)
    hicon = user32.CreateIconIndirect(ctypes.byref(info))
    gdi32.DeleteObject(colour_bmp)
    gdi32.DeleteObject(mask_bmp)
    return hicon or user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))


def preview(soc, fraction=None, size=16) -> str:
    """ASCII rendering of the glyph, so proportions can be checked without eyes
    on a 16 px taskbar."""
    unit = size / 16.0
    px = bytearray(size * size * 4)
    text = "--" if soc is None else str(int(round(soc)))
    bar_h = max(2, round(2 * unit))
    bar_top = size - bar_h

    def width_at(sc):
        return len(text) * GLYPH_W * sc + (len(text) - 1) * sc

    scale = 1
    while (GLYPH_H * (scale + 1) <= bar_top - 1
           and width_at(scale + 1) <= size - 2):
        scale += 1
    _draw_text(px, size, text, (255, 255, 255), FILL_ALPHA, scale,
               max(0, (bar_top - GLYPH_H * scale) // 2))
    _rect(px, size, 0, bar_top, size, size, (255, 255, 255), EMPTY_ALPHA)
    if fraction is None and soc is not None:
        fraction = soc / 100.0
    if fraction is not None:
        filled = max(0, min(size, int(round(size * fraction))))
        _rect(px, size, 0, bar_top, filled, size, (255, 255, 255), FILL_ALPHA)
    out = []
    for y in range(size):
        row = ""
        for x in range(size):
            a = px[(y * size + x) * 4 + 3]
            row += "#" if a == FILL_ALPHA else ("." if a else " ")
        out.append(row)
    return "\n".join(out)


# -- reading a state into something displayable -------------------------------

class Reading:
    """What the tray knows right now, and how sure it is."""

    def __init__(self, state: dict | None, paused_by_other: bool = False,
                 handover_s: float = 0.0):
        self.state = state or {}
        self.paused = paused_by_other
        self.handover_s = handover_s
        self.flow = self.state.get("flow") or {}
        self.stale = bool(self.state.get("stale")) or not self.state
        self.soc = self.state.get("soc")
        self.trust_soc = bool(self.state.get("trust_soc"))

    @property
    def fraction(self):
        return None if self.soc is None else self.soc / 100.0

    @property
    def colour(self):
        if self.handover_s > 0:
            return GREY
        if self.stale or not self.flow:
            return GREY
        if self.flow.get("on_battery"):
            soc = self.soc
            volts = self.flow.get("batt_v") or 0
            # Voltage is the honest low-battery signal; SOC only refines it.
            if volts and volts <= 47.0:
                return RED
            if soc is not None and soc < 20:
                return RED
            if soc is not None and soc < 45:
                return ORANGE
            return AMBER
        if (self.flow.get("batt_w") or 0) > 20:
            return GREEN
        if self.flow.get("grid_present"):
            return BLUE
        return RED

    def headline(self) -> str:
        if self.handover_s > 0:
            return f"Lent to WatchPower - {self.handover_s / 60:.0f} min left"
        if self.paused:
            return "Paused - another task has the link"
        if not self.state:
            return "No data - is collector.py running?"
        if self.stale:
            return f"Stale - last reading {self.state.get('age_s')}s ago"
        if self.flow.get("on_battery"):
            return f"ON BATTERY - {self.flow.get('load_w', 0):.0f} W draw"
        if (self.flow.get("batt_w") or 0) > 20:
            return f"On grid, charging - {self.flow.get('load_w', 0):.0f} W load"
        if self.flow.get("grid_present"):
            return f"On grid - {self.flow.get('load_w', 0):.0f} W load"
        return "No grid"

    def tooltip(self) -> str:
        """szTip is 128 wchars, so this stays tight."""
        lines = ["Phantom II - " + self.headline()[:60]]
        if self.flow:
            soc_text = "--" if self.soc is None else f"{self.soc}%"
            basis = "" if self.trust_soc else " est"
            lines.append(f"Batt {soc_text}{basis}  {self.flow.get('batt_v', 0):.1f} V")
            lines.append(f"Load {self.flow.get('load_w', 0):.0f} W   "
                         f"PV {self.flow.get('pv_w', 0):.0f} W")
            runtime = self.flow.get("runtime_h")
            if self.flow.get("on_battery") and runtime:
                lines.append(f"~{runtime:.1f} h remaining")
        warnings = self.state.get("warnings") or []
        if warnings:
            lines.append("! " + ", ".join(warnings)[:40])
        if self.state.get("local"):
            lines.append(self.state["local"][11:16] + "  " +
                         ("cached" if self.stale else self.state.get("source", "")))
        return "\n".join(lines)[:127]


# -- the resident process -----------------------------------------------------

ID_REFRESH, ID_LOGS, ID_SYNC, ID_RESTORE, ID_AUTOSTART, ID_EXIT = range(1, 7)
ID_HANDOVER, ID_TAKEBACK = 7, 8

# How long the Android app gets the dongle for when you ask. It reads the
# vendor cloud rather than the inverter, so it needs a couple of minutes of
# uploads before it shows anything current -- a short loan is wasted.
HANDOVER_MINUTES = 15


class Monitor:
    """Samples in the background and tells the window when there is news."""

    def __init__(self, hwnd, mode: str, direct_ip: str | None, log):
        self.hwnd = hwnd
        self.mode = mode                 # "follow" | "direct" | "redirect"
        self.direct_ip = direct_ip
        self.log = log
        self.idle = arbiter.Idle()
        self.reading = Reading(None)
        self._lock = threading.Lock()
        self._claim = arbiter.LinkClaim()
        self._conn = None
        self._qpiri = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.idle.stop()
        self._drop_link()
        self._claim.close()

    def wake(self) -> None:
        self.idle.wake()

    def snapshot(self) -> Reading:
        with self._lock:
            return self.reading

    def owns_link(self) -> bool:
        return self._conn is not None

    def _publish(self, reading: Reading) -> None:
        with self._lock:
            self.reading = reading
        user32.PostMessageW(self.hwnd, WM_APP_SAMPLE, 0, 0)

    def _drop_link(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self._claim.release()

    def _run(self) -> None:
        while not self.idle.stopped:
            try:
                self._sample_once()
            except Exception as exc:                 # never let the loop die
                self.log({"event": "tray-error", "error": repr(exc)})
                self._drop_link()
                self._publish(Reading(bridge.read_state()))
            interval = (config.BATTERY_POLL_INTERVAL
                        if self.snapshot().flow.get("on_battery")
                        else max(config.POLL_INTERVAL, 5))
            if self.mode == "follow":
                interval = max(interval, 5)
            if not self.idle.wait(interval):
                break

    def _sample_once(self) -> None:
        loan = bridge.handover_remaining()
        if loan > 0:
            # The collector has given the dongle back to the cloud; there is
            # nothing to read and nothing to fight over until it expires.
            self._drop_link()
            self._publish(Reading(bridge.read_state(), handover_s=loan))
            return

        if self.mode == "follow":
            self._publish(Reading(bridge.read_state()))
            return

        # Owning modes: the foreground wins. If the mutex is taken, drop our
        # session and read the state file rather than fighting for the bus.
        if not self._claim.try_acquire(0):
            self._drop_link()
            self._publish(Reading(bridge.read_state(), paused_by_other=True))
            return

        if self._conn is None:
            self._conn = bridge.open_link(
                direct=self.direct_ip,
                redirect=(self.mode == "redirect"),
                wait=30.0, log=self.log, announce=lambda *a: None)
            self._qpiri = bridge.read(self._conn, "QPIRI", self.log).get("data")

        snap = bridge.snapshot(self._conn, self.log, qpiri=self._qpiri)
        bridge.write_state(snap, source="tray")
        self._publish(Reading(bridge.read_state()))


class TrayWindow:
    """The icon, its menu, and the message pump that drives both."""

    def __init__(self, mode: str, direct_ip: str | None, log):
        self._log = log
        self._icon = None
        self._added = False

        # Held as an attribute because ctypes does not keep the trampoline
        # alive on its own -- let it be collected and the first message into
        # the window proc jumps into freed memory.
        self._wndproc = WNDPROC(self._on_message)

        # Explorer broadcasts this when it restarts and rebuilds the taskbar.
        # Without re-adding the icon on it, the icon disappears for good the
        # first time Explorer restarts, which looks exactly like a crash. The
        # window is a normal top-level one, not message-only: message-only
        # windows are excluded from broadcasts and would cost us the recovery.
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

        self._hwnd = self._make_window()
        self._monitor = Monitor(self._hwnd, mode, direct_ip, log)

    def _make_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = hinst
        cls.lpszClassName = "PhantomBridgeTray"
        if not user32.RegisterClassExW(ctypes.byref(cls)):
            err = ctypes.get_last_error()
            if err != 1410:                      # ERROR_CLASS_ALREADY_EXISTS
                raise ctypes.WinError(err)
        self._class = cls                        # keep class (and proc) alive
        hwnd = user32.CreateWindowExW(0, "PhantomBridgeTray", "phantom-bridge",
                                      0, 0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        return hwnd

    # -- icon -------------------------------------------------------------

    def _nid(self, flags):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_APP_TRAY
        return nid

    def _apply(self) -> None:
        reading = self._monitor.snapshot()
        icon = make_icon(reading.colour, reading.fraction, reading.soc)
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.hIcon = icon
        nid.szTip = reading.tooltip()
        ok = shell32.Shell_NotifyIconW(NIM_MODIFY if self._added else NIM_ADD,
                                       ctypes.byref(nid))
        self._added = bool(ok) or self._added
        # Replace first, then destroy the old one -- never the other way round,
        # and never skipped: one leaked HICON per sample is a handle leak.
        old, self._icon = self._icon, icon
        if old:
            user32.DestroyIcon(old)

    def _remove_icon(self) -> None:
        if self._added:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
            self._added = False
        if self._icon:
            user32.DestroyIcon(self._icon)
            self._icon = None

    # -- menu -------------------------------------------------------------

    def _show_menu(self) -> None:
        reading = self._monitor.snapshot()
        menu = user32.CreatePopupMenu()

        user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0,
                           reading.headline()[:52])
        if reading.flow:
            volts = reading.flow.get("batt_v", 0)
            soc = "--" if reading.soc is None else f"{reading.soc}%"
            basis = "" if reading.trust_soc else "  (voltage estimate)"
            user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0,
                               f"   {soc} at {volts:.2f} V{basis}"[:52])
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)

        user32.AppendMenuW(menu, MF_STRING, ID_REFRESH, "Refresh now")
        user32.AppendMenuW(menu, MF_STRING, ID_LOGS, "Open logs folder")

        if self._monitor.owns_link():
            user32.AppendMenuW(menu, MF_STRING, ID_SYNC,
                               "Sync clock from internet")
        else:
            user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0,
                               "Clock synced by collector (on connect, then 24h)")
        if reading.handover_s > 0:
            user32.AppendMenuW(menu, MF_STRING, ID_TAKEBACK,
                               "Take the link back now")
        else:
            user32.AppendMenuW(
                menu, MF_STRING, ID_HANDOVER,
                f"Lend to WatchPower app ({HANDOVER_MINUTES} min)")
        user32.AppendMenuW(menu, MF_STRING, ID_RESTORE,
                           "Restore vendor cloud and stop")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu,
                           MF_STRING | (MF_CHECKED if autostart_enabled() else 0),
                           ID_AUTOSTART, "Start at login")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "Exit")

        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # Required, or the menu will not dismiss when focus moves elsewhere.
        user32.SetForegroundWindow(self._hwnd)
        choice = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                       point.x, point.y, 0, self._hwnd, None)
        user32.DestroyMenu(menu)
        if choice:
            self._on_command(choice)

    def _on_command(self, ident: int) -> None:
        if ident == ID_REFRESH:
            self._monitor.wake()
        elif ident == ID_LOGS:
            bridge.LOG_DIR.mkdir(exist_ok=True)
            os.startfile(str(bridge.LOG_DIR))
        elif ident == ID_SYNC:
            threading.Thread(target=self._sync_clock, daemon=True).start()
        elif ident == ID_RESTORE:
            threading.Thread(target=self._restore, daemon=True).start()
        elif ident == ID_HANDOVER:
            bridge.request_handover(HANDOVER_MINUTES)
            self._monitor.wake()
        elif ident == ID_TAKEBACK:
            bridge.cancel_handover()
            self._monitor.wake()
        elif ident == ID_AUTOSTART:
            set_autostart(not autostart_enabled())
        elif ident == ID_EXIT:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    def _sync_clock(self) -> None:
        conn = getattr(self._monitor, "_conn", None)
        if conn is None:
            return
        bridge.clock_sync(conn, self._log, announce=lambda *a: None)

    def _restore(self) -> None:
        bridge.restore(announce=lambda *a: None)
        self._log({"event": "restore", "by": "tray"})

    # -- messages ---------------------------------------------------------

    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == self._taskbar_created:
            self._added = False
            self._apply()
            return 0
        if msg == WM_APP_TRAY:
            if lparam in (WM_RBUTTONUP, WM_LBUTTONUP):
                self._show_menu()
            return 0
        if msg == WM_APP_SAMPLE:
            self._apply()
            return 0
        if msg == WM_COMMAND:
            self._on_command(wparam & 0xFFFF)
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self) -> None:
        self._apply()
        self._monitor.start()
        msg = wintypes.MSG()
        # GetMessageW parks this thread in the kernel until a message arrives.
        # It is not scheduled and costs nothing while waiting.
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self._monitor.stop()
        self._remove_icon()


# -- autostart ----------------------------------------------------------------

def autostart_enabled() -> bool:
    return bridge.autostart_enabled(RUN_VALUE)


def set_autostart(enable: bool) -> None:
    bridge.set_autostart(RUN_VALUE, enable, "tray.py")


def refresh_autostart_path() -> None:
    bridge.refresh_autostart(RUN_VALUE, "tray.py")


# -- entry point --------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--direct", metavar="IP",
                       help="own the link by connecting out to the dongle")
    group.add_argument("--redirect", action="store_true",
                       help="own the link by redirecting the dongle to us")
    args = parser.parse_args()

    mode = "direct" if args.direct else ("redirect" if args.redirect else "follow")
    log = bridge.quiet_logger()
    refresh_autostart_path()
    TrayWindow(mode, args.direct, log).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
