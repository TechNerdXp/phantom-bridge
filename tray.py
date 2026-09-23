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
there is. The *colour* is a ladder of concern set by the owner (see the
comment over the palette): how hard the pack is being drawn, whether it is
carrying the house alone, and whether it is still full, with the low bands
on top of everything and the battery voltage as their backstop, since 47 V
is a low pack whatever the percentage claims. The bands are in config.py
(TRAY_LOW_SOC, TRAY_CRITICAL_SOC, TRAY_FULL_SOC, TRAY_HEAVY_DISCHARGE_A). The tooltip
always states the basis and carries the battery voltage, which is the honest
number. Set TRUST_SOC in config.py once BMS comms are confirmed and the
caveat goes away.
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
import flow as flowmod
import config
import history
import policy
import watch

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
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_WARNING = 0x01, 0x02

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
user32.SetTimer.restype = ctypes.c_size_t
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
WM_TIMER, LIMIT_TIMER = 0x0113, 2

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

# The colour is a ladder of concern, written by the owner (2026-09-20) and
# read here as three concerns that add up, one step each:
#   the pack is not full          (SOC under TRAY_FULL_SOC)
#   the draw is heavy             (TRAY_HEAVY_DISCHARGE_A or more out)
#   the house is on the pack alone (flow.source == "battery": no sun in it,
#                                   the grid not carrying -- present or not)
#   0 concerns GREEN, 1 BLUE, 2 ORANGE, 3 RED.
# So a full pack under a light draw with the sun still in it is green; the
# same draw once the pack is below full is blue; heavy on a part-drained pack
# is orange, as is carrying the house alone; heavy and alone below full is
# red. Charging and resting are green: nothing is coming out of the pack.
# On top of the ladder, level wins: under TRAY_LOW_SOC is orange and under
# TRAY_CRITICAL_SOC, or the pack voltage at TRAY_CRITICAL_V, is red,
# whatever the pack is doing. Grey is no data.
# Green, red and grey are RouterOps' tray colours (its tray.py keeps
# them as BGR; these are the same values as RGB), because the two icons sit
# side by side and one green must mean "good" in both.
GREEN = (76, 175, 80)       # no concern: charging, resting, or full and light
BLUE = (100, 170, 235)      # one concern
ORANGE = (255, 120, 40)     # two concerns, or low
RED = (229, 57, 53)         # three concerns, or critical
GREY = (158, 158, 158)      # no data

SHELL_ALPHA = 200           # battery outline
FILL_ALPHA = 255
EMPTY_ALPHA = 55            # unfilled interior: visible on both taskbar themes


def _icon_size() -> int:
    return user32.GetSystemMetrics(SM_CXSMICON) or 16


# A 5-row pixel font, 3 px wide except the "1", which is a single column.
# The narrow 1 is what lets "100" take the same scale as "73" at 16 px: at
# scale 2 with 1 px gaps it is exactly 16 px wide, where a 3-wide 1 forced it
# down to scale 1 -- a 5 px tall number at the one value that deserves to
# look full. The real percentage is drawn, never abbreviated: that is the
# whole point of putting the number on the icon rather than in the tooltip.
DIGITS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("1", "1", "1", "1", "1"),
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
GLYPH_H = 5


def _gap(scale: int) -> int:
    """Air between glyphs: half the pixel scale, never less than a pixel."""
    return max(1, scale // 2)


def _text_width(text: str, scale: int) -> int:
    return (sum(len(DIGITS[c][0]) for c in text if c in DIGITS) * scale
            + (len(text) - 1) * _gap(scale))


def _fit_scale(text: str, size: int, bar_top: int) -> int:
    """Largest pixel scale whose glyphs fit above the bar, with a pixel of
    air over it, and across the full icon width."""
    scale = 1
    while (GLYPH_H * (scale + 1) <= bar_top - 1
           and _text_width(text, scale + 1) <= size):
        scale += 1
    return scale


def _rect(px, size, x0, y0, x1, y1, colour, alpha) -> None:
    """Fill an inclusive-exclusive rect in a top-down BGRA buffer."""
    for y in range(max(0, y0), min(size, y1)):
        row = y * size * 4
        for x in range(max(0, x0), min(size, x1)):
            off = row + x * 4
            px[off:off + 4] = bytes((colour[2], colour[1], colour[0], alpha))


def _draw_text(px, size, text, colour, alpha, scale, top) -> None:
    """Centre a run of glyphs horizontally at `top`, scaled by `scale`."""
    ox = (size - _text_width(text, scale)) // 2
    for char in text:
        glyph = DIGITS.get(char)
        if glyph is None:
            continue
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    x = ox + col * scale
                    y = top + row * scale
                    _rect(px, size, x, y, x + scale, y + scale, colour, alpha)
        ox += len(glyph[0]) * scale + _gap(scale)


def make_icon(colour, fraction, soc=None, size=None, alert: bool = False) -> int:
    """The state-of-charge number, plus a proportional bar beneath it.

    `alert` is the at-the-limit mark: a small red square in the top-right
    corner, and nothing else. The digits and the bar stay the battery's
    own, in their own colour, so the icon keeps its shape and its meaning
    and only gains a signal. The tray alternates it with the plain icon
    every half second while a leg is at its limit.

    The number is the thing being asked for, so it gets the space: digits fill
    the upper two thirds and the bar is a 2 px strip under them. Both derive
    from `size`, so a 20, 24 or 32 px icon on a high-DPI taskbar is drawn at
    its own scale rather than being a 16 px drawing stretched.

    Caller owns the returned HICON and must DestroyIcon it.
    """
    size = size or _icon_size()
    px = render_icon(colour, fraction, soc, size, alert)

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


def render_icon(colour, fraction, soc=None, size: int = 16,
                alert: bool = False) -> bytearray:
    """The icon as pixels: a top-down BGRA buffer, `size` square.

    Split from `make_icon` so the same drawing can go somewhere other than
    an HICON -- icon.py renders it into the .ico the frozen exe wears, so
    the file in Explorer and the glyph in the tray are one drawing.
    """
    unit = size / 16.0
    px = bytearray(size * size * 4)          # transparent to start

    text = "--" if soc is None else str(int(round(soc)))

    # The bar is measured first so the digits can take whatever is left, which
    # is what keeps the two in proportion at any icon size.
    bar_h = max(2, round(2 * unit))
    bar_top = size - bar_h

    scale = _fit_scale(text, size, bar_top)

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
    if alert:
        mark = max(3, round(3 * unit))
        _rect(px, size, size - mark, 0, size, mark, RED, 255)
    return px


def preview(soc, fraction=None, size=16) -> str:
    """ASCII rendering of the glyph, so proportions can be checked without eyes
    on a 16 px taskbar: '#' drawn, '.' the empty part of the bar."""
    if fraction is None and soc is not None:
        fraction = soc / 100.0
    px = render_icon((255, 255, 255), fraction, soc, size)
    alpha = px[3::4]
    return "\n".join(
        "".join("#" if a == FILL_ALPHA else "." if a else " "
                for a in alpha[y * size:(y + 1) * size])
        for y in range(size))


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
        # Legs at their limit -- grid line, home lines, battery -- from the
        # same rule the watch screen uses.
        self.at_limit = (flowmod.at_limit(flowmod.leg_stress(self.flow, config))
                         if self.flow and not self.stale else [])

    @property
    def fraction(self):
        return None if self.soc is None else self.soc / 100.0

    @property
    def colour(self):
        if self.handover_s > 0:
            return GREY
        if self.stale or not self.flow:
            return GREY
        soc = self.soc
        volts = self.flow.get("batt_v") or 0
        # Low wins over everything else. Voltage is the honest low signal
        # and backs the percentage up: 47 V is a low pack whatever the
        # voltage-derived SOC claims.
        if volts and volts <= config.TRAY_CRITICAL_V:
            return RED
        if soc is not None and soc < config.TRAY_CRITICAL_SOC:
            return RED
        if soc is not None and soc < config.TRAY_LOW_SOC:
            return ORANGE
        discharge_a = self.flow.get("batt_discharge_a") or 0
        if discharge_a <= 0:
            return GREEN                     # charging or resting
        concerns = ((soc is not None and soc < config.TRAY_FULL_SOC)
                    + (discharge_a >= config.TRAY_HEAVY_DISCHARGE_A)
                    + (self.flow.get("source") == "battery"))
        return (GREEN, BLUE, ORANGE, RED)[concerns]

    def headline(self) -> str:
        if self.handover_s > 0:
            return f"Lent to WatchPower - {self.handover_s / 60:.0f} min left"
        if self.paused:
            return "Paused - another task has the link"
        if not self.state:
            return "No data - is collector.py running?"
        if self.stale:
            return f"Stale - last reading {self.state.get('age_s')}s ago"
        load = f"{self.flow.get('load_w', 0):.0f} W"
        source = self.flow.get("source")
        if self.flow.get("on_battery"):
            who = "Solar + battery" if source == "solar+battery" else "ON BATTERY"
            if not self.flow.get("grid_present", True):
                who = "GRID OUT, on battery"
            return f"{who} - {load} draw"
        charging = (self.flow.get("batt_w") or 0) > 20
        if source == "solar":
            return f"On solar{', charging' if charging else ''} - {load} load"
        if self.flow.get("grid_present"):
            return f"On grid{', charging' if charging else ''} - {load} load"
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
            today = self.state.get("today") or {}
            if today.get("pv_wh") is not None or today.get("load_wh") is not None:
                pv_kwh = (today.get("pv_wh") or 0) / 1000
                load_kwh = (today.get("load_wh") or 0) / 1000
                lines.append(f"Today {pv_kwh:.1f} kWh solar, {load_kwh:.1f} kWh load")
            runtime = self.flow.get("runtime_h")
            if self.flow.get("on_battery") and runtime:
                lines.append(f"~{runtime:.1f} h remaining")
        if self.at_limit:
            names = {"grid": "grid line", "home": "home lines", "battery": "battery"}
            lines.append("! " + " and ".join(names[n] for n in self.at_limit) + " at the limit")
        warnings = self.state.get("warnings") or []
        if warnings:
            lines.append("! " + ", ".join(warnings)[:40])
        if self.state.get("local"):
            lines.append(self.state["local"][11:16] + "  " +
                         ("cached" if self.stale else self.state.get("source", "")))
        return "\n".join(lines)[:127]


# -- the resident process -----------------------------------------------------

ID_REFRESH, ID_LOGS, ID_SYNC, ID_RESTORE, ID_AUTOSTART, ID_EXIT = range(1, 7)
ID_HANDOVER, ID_TAKEBACK, ID_WATCH, ID_HISTORY = 7, 8, 9, 11

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
        self._watch = watch.WatchWindow()
        self._history = history.HistoryWindow()
        self._watch.on_history = self._history.open
        self._was_on_battery = None
        self._limit_blinking = False     # the at-the-limit timer is running
        self._limit_phase = True         # which half of the blink we are in

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
        self._limit_timer(bool(reading.at_limit))
        icon = make_icon(reading.colour, reading.fraction, reading.soc,
                         alert=bool(reading.at_limit) and self._limit_phase)
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.hIcon = icon
        nid.szTip = reading.tooltip()
        ok = shell32.Shell_NotifyIconW(NIM_MODIFY if self._added else NIM_ADD,
                                       ctypes.byref(nid))
        self._added = bool(ok) or self._added
        # The watch window wears the same glyph on its title bar. Set it there
        # before the old handle goes, for the same reason as below.
        self._watch.set_icon(icon)
        self._history.set_icon(icon)
        # Replace first, then destroy the old one -- never the other way round,
        # and never skipped: one leaked HICON per sample is a handle leak.
        old, self._icon = self._icon, icon
        if old:
            user32.DestroyIcon(old)

    def _notify(self, title: str, text: str, warning: bool = False) -> None:
        """A native balloon. Windows already provides this; no toast library."""
        if not self._added:
            return
        nid = self._nid(NIF_INFO)
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = NIIF_WARNING if warning else NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _announce_transition(self) -> None:
        """Tell the user the moment the grid goes, and when it returns.

        Off by default (config.TRAY_NOTIFICATIONS): the inverter flaps between
        line and battery mode every few seconds around dawn, and each flip was
        a balloon. Kept for a site where a flip really is an outage.
        """
        if not config.TRAY_NOTIFICATIONS:
            return
        reading = self._monitor.snapshot()
        if not reading.flow or reading.stale:
            return
        now_on_battery = bool(reading.flow.get("on_battery"))
        if self._was_on_battery is None:
            self._was_on_battery = now_on_battery
            return
        if now_on_battery == self._was_on_battery:
            return
        self._was_on_battery = now_on_battery
        if now_on_battery:
            detail = f"Drawing {reading.flow.get('load_w', 0):.0f} W from the pack"
            runtime = reading.flow.get("runtime_h")
            if runtime:
                detail += f", about {runtime:.1f} h left"
            self._notify("Grid lost - on battery", detail, warning=True)
        else:
            self._notify("Grid back",
                         f"On utility again, {reading.flow.get('load_w', 0):.0f} W load")

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
        plan = policy.headline(reading.state.get("policy"))
        if plan:
            user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, ("   " + plan)[:52])
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)

        user32.AppendMenuW(menu, MF_STRING, ID_WATCH, "Open watch screen")
        user32.AppendMenuW(menu, MF_STRING, ID_HISTORY, "Power history")
        user32.AppendMenuW(menu, MF_STRING, ID_REFRESH, "Refresh now")
        user32.AppendMenuW(menu, MF_STRING, ID_LOGS, "Open logs folder")

        if self._monitor.owns_link():
            user32.AppendMenuW(menu, MF_STRING, ID_SYNC,
                               "Sync clock from internet")
        else:
            user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0,
                               "Clock synced by collector (on connect, then hourly)")
        if reading.handover_s > 0:
            user32.AppendMenuW(menu, MF_STRING, ID_TAKEBACK,
                               "Take the link back now")
        else:
            user32.AppendMenuW(
                menu, MF_STRING, ID_HANDOVER,
                f"Lend to WatchPower app ({HANDOVER_MINUTES} min)")
        # Following, the collector owns the link and re-sends its redirect
        # within half a minute of losing it, so a restore from here stops
        # nothing -- the loan above is what hands the dongle over. Only a
        # tray that owns the link can restore and stop.
        if self._monitor.mode != "follow":
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
        if ident == ID_WATCH:
            self._watch.open()
        elif ident == ID_HISTORY:
            self._history.open()
        elif ident == ID_REFRESH:
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

    def _limit_timer(self, wanted: bool) -> None:
        """A 500 ms timer that exists only while a leg is at its limit. It
        alternates the icon between plain and marked; a calm tray has no
        timer at all."""
        if wanted and not self._limit_blinking:
            self._limit_blinking = bool(user32.SetTimer(self._hwnd, LIMIT_TIMER, 500, None))
        elif not wanted and self._limit_blinking:
            user32.KillTimer(self._hwnd, LIMIT_TIMER)
            self._limit_blinking, self._limit_phase = False, True

    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_TIMER and wparam == LIMIT_TIMER:
            self._limit_phase = not self._limit_phase
            self._apply()
            return 0
        if msg == self._taskbar_created:
            self._added = False
            self._apply()
            return 0
        if msg == WM_APP_TRAY:
            if lparam == WM_LBUTTONUP:
                self._watch.open()
            elif lparam == WM_RBUTTONUP:
                self._show_menu()
            return 0
        if msg == WM_APP_SAMPLE:
            self._apply()
            self._watch.refresh()
            self._history.refresh()
            self._announce_transition()
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
    # Before any window or icon exists, or Windows stretches both.
    watch.set_dpi_aware()
    log = bridge.quiet_logger()
    refresh_autostart_path()
    TrayWindow(mode, args.direct, log).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
