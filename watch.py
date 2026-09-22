#!/usr/bin/env python3
"""The watch screen: a native window, painted with GDI and GDI+.

Opened from the tray icon (left-click, or "Open watch screen"). It lives in
the tray's own process, so it is not a service, has no port, and starts no
second interpreter.

    python watch.py                  standalone, for looking at it
    python watch.py --png out.png    render one frame to a file and exit

Why native rather than a browser or a console:

  A browser tab costs 100-300 MB to display a page that is 800 bytes of JSON.
  This is a window in a process that is already running -- a few MB of bitmap
  and a handful of GDI fonts.

  It repaints on WM_APP_SAMPLE, meaning once per collector sample and only
  while the window is actually open. A closed window costs exactly nothing:
  there is no timer, no poll, no layout, no thread.

The picture is the four pillars of the energy balance -- solar above, grid
left, home right, battery below -- around the inverter, with an arrow on each
leg whose direction is the direction of the current and whose thickness is
the watts, drawn to ONE shared scale, so a fat arrow means the same thing on
every leg. Watts lead at every pillar; the volts, amps and hertz sit under
them in a small monospace face for whoever wants them. The battery pillar is
a round gauge, and one line of status under the board. The day's figures
and the cable bar that used to sit there live in the history window (click
"history >" at the bottom, or the tray menu); the arrows carry the limits.

Under the board is the day's rule: one horizontal 24-hour scale carrying
the one thing the board cannot show -- which way the house is pointed and
when that changes. It runs **sunrise to sunrise**, sunrise being the
turnaround: the moment the pack stops falling and the sun starts pushing
it back up. That is the plan's own day, so the night is one stretch rather
than two halves either side of midnight, and both cuts are on the scale.

Left of the now marker is the record, what the inverter was actually set
to, from the tape the collector publishes; right of it is the plan, the
same colours in a lighter key. The
cuts are the two switches of the owner's rule: SBU -> SUB at dusk, when the
pack becomes the outage reserve, and SUB -> SBU at the release. A time the
plan has not worked out yet wears a "~"; a stretch nothing can put a clock
on (the floor hold) fades out instead of claiming one; a red underline
marks where the inverter disagreed with the plan.

GDI+ (gdiplus.dll, Windows XP and later) draws the arrows, ring and icons
anti-aliased. It is loaded lazily and, if it is missing, plain GDI draws the
same shapes without the smoothing -- the window never fails to open.

Backward compatibility, deliberately: GetDpiForSystem and
SetProcessDpiAwarenessContext are Windows 10 1607/1703+, so they are looked
up inside try/except at call time and fall back to GetDeviceCaps. Never
resolve a version-gated export at module import: a missing one raises
AttributeError and takes the whole tray down on an older machine.
"""
from __future__ import annotations

import ctypes
import math
import pathlib
import struct
import sys
import time
import zlib
from ctypes import wintypes

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

import bridge
import config
import flow as flowmod
import policy

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
HGDIOBJ = wintypes.HANDLE

WM_DESTROY, WM_CLOSE, WM_PAINT, WM_ERASEBKGND = 0x0002, 0x0010, 0x000F, 0x0014
WM_APP_SAMPLE = 0x8002
WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
WM_LBUTTONDOWN = 0x0201
WM_TIMER, BLINK_TIMER = 0x0113, 1
WS_OVERLAPPED, WS_CAPTION, WS_SYSMENU, WS_MINIMIZEBOX = 0x0, 0xC00000, 0x80000, 0x20000
WS_VISIBLE = 0x10000000
SW_SHOW, SW_HIDE, SW_RESTORE = 5, 0, 9
DT_LEFT, DT_RIGHT, DT_CENTER = 0x0, 0x2, 0x1
DT_SINGLELINE, DT_VCENTER, DT_NOPREFIX = 0x20, 0x4, 0x800
TRANSPARENT = 1
SRCCOPY = 0x00CC0020
PS_SOLID, NULL_PEN, NULL_BRUSH = 0, 8, 5
AD_CLOCKWISE = 2

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL),
                ("rcPaint", wintypes.RECT), ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


# Declared without exception: an undeclared handle argument is a C int to
# ctypes, and a 64-bit HDC or HBRUSH does not fit in one.
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HWND, wintypes.HANDLE,
                                   wintypes.HINSTANCE, wintypes.LPVOID]
user32.BeginPaint.restype = wintypes.HDC
user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.AdjustWindowRectEx.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD,
                                      wintypes.BOOL, wintypes.DWORD]
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                            wintypes.HBRUSH]
user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                             ctypes.POINTER(wintypes.RECT), wintypes.UINT]
user32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.SendMessageW.restype = LRESULT
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                wintypes.LPARAM]
user32.SetTimer.restype = ctypes.c_size_t
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.CreatePen.restype = wintypes.HANDLE
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
gdi32.GetStockObject.restype = HGDIOBJ
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER),
                                   wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, HGDIOBJ]
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         wintypes.DWORD]
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.CreateFontW.restype = wintypes.HFONT
gdi32.CreateFontW.argtypes = [ctypes.c_int] * 8 + [wintypes.DWORD] * 5 + [wintypes.LPCWSTR]
gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.GetDeviceCaps.restype = ctypes.c_int
gdi32.Polygon.argtypes = [wintypes.HDC, ctypes.POINTER(POINT), ctypes.c_int]
gdi32.Polyline.argtypes = [wintypes.HDC, ctypes.POINTER(POINT), ctypes.c_int]
gdi32.Ellipse.argtypes = [wintypes.HDC] + [ctypes.c_int] * 4
gdi32.Arc.argtypes = [wintypes.HDC] + [ctypes.c_int] * 8
gdi32.SetArcDirection.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.GdiFlush.restype = wintypes.BOOL
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


def set_dpi_aware() -> None:
    """Tell Windows we scale ourselves, before any window exists.

    Without this the panel is bitmap-stretched by the compositor on any
    display above 100%, which on a Windows 10 laptop is most of them -- every
    label goes soft and the crisp GDI text is wasted. Per-monitor v2 is
    Windows 10 1703+; the older system-wide call is the fallback.
    """
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


LOGPIXELSX = 88


def system_scale() -> float:
    """Display scale as a multiplier, 1.0 at 96 DPI.

    Three tiers, newest first, because this has to run on whatever Windows is
    in front of it: GetDpiForSystem (10 1607+), then GetDeviceCaps on the
    screen DC, which has worked since Windows 95 and is accurate once the
    process is DPI aware, then 1.0.
    """
    try:
        user32.GetDpiForSystem.restype = wintypes.UINT
        dpi = user32.GetDpiForSystem()
        if dpi:
            return dpi / 96.0
    except (AttributeError, OSError):
        pass
    hdc = user32.GetDC(None)
    if hdc:
        try:
            dpi = gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
            if dpi:
                return dpi / 96.0
        finally:
            user32.ReleaseDC(None, hdc)
    return 1.0


# -- palette ------------------------------------------------------------------
# Kept as (r, g, b) because GDI wants BGR packed and GDI+ wants ARGB.

BG        = (0x12, 0x14, 0x1A)
PANEL     = (0x1E, 0x22, 0x2B)
LINE      = (0x2C, 0x32, 0x3E)
INK       = (0xE8, 0xEC, 0xF3)
INK_DIM   = (0x8B, 0x93, 0xA3)
INK_FAINT = (0x55, 0x5C, 0x6A)
# Whose power it is, not what the wire is (owner, 2026-09-22): the sun is
# green, the pack is teal because it is yesterday's sun -- nothing here
# charges from the grid -- and the utility is orange, the one you pay for.
# The house keeps its own violet. Used by the watch screen, the rule and the
# history window alike; the tray icon has its own ladder in tray.py, which
# matches RouterOps and must not follow this.
SOLAR     = (0x52, 0xC8, 0x6E)
GRID      = (0xE0, 0x82, 0x35)
BATT      = (0x35, 0xC2, 0xAE)
LOAD      = (0xA7, 0x8B, 0xFA)
WARN      = (0xF2, 0x9A, 0x3E)
BAD       = (0xE2, 0x57, 0x4C)

# The rule borrows those three and adds one: blue is the pack carrying the
# house inside a SUB stretch, which only happens when the grid has gone out
# -- the setting says grid, the outage says otherwise.
LANE_OUTAGE = (0x4F, 0x9F, 0xE8)


def cref(rgb) -> int:
    r, g, b = rgb
    return r | (g << 8) | (b << 16)


def argb(rgb, alpha=255) -> int:
    r, g, b = rgb
    return (alpha << 24) | (r << 16) | (g << 8) | b


# The header colour follows flow.source -- who is carrying the house -- not
# QMOD's letter. Mapping B to "ON BATTERY" was the screen's one real lie: the
# unit sits in B all day on solar with the pack charging.
SOURCE_COLOURS = {"solar": SOLAR, "solar+battery": BATT, "battery": BATT,
                  "grid": GRID, "fault": BAD}

UI_FACE, MONO_FACE = "Segoe UI", "Consolas"


def _watts(value) -> str:
    if value is None:
        return "--"
    return f"{value:,.0f} W"


def _kwh(wh) -> str:
    return "--" if wh is None else f"{wh / 1000:.1f} kWh"


def _hours(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 60:
        return "0 min"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


# -- GDI+ ---------------------------------------------------------------------

class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_uint32),
                ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", wintypes.BOOL),
                ("SuppressExternalCodecs", wintypes.BOOL)]


class PointF(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float)]


_gdiplus = None            # None = not tried, False = unavailable, else the DLL
_gdiplus_token = None


def _load_gdiplus():
    """gdiplus.dll, started once per process. False if it cannot be had.

    Every argtype is declared: the handles are pointers, and an undeclared
    pointer argument is truncated to a C int on 64-bit Windows.
    """
    global _gdiplus, _gdiplus_token
    if _gdiplus is not None:
        return _gdiplus
    try:
        lib = ctypes.WinDLL("gdiplus")
        F, P, I = ctypes.c_float, ctypes.c_void_p, ctypes.c_int
        PP, PF = ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(PointF)
        signatures = {
            "GdiplusStartup": [PP, ctypes.POINTER(GdiplusStartupInput), P],
            "GdipCreateFromHDC": [wintypes.HDC, PP],
            "GdipDeleteGraphics": [P],
            "GdipSetSmoothingMode": [P, I],
            "GdipCreatePen1": [ctypes.c_uint32, F, I, PP],
            "GdipSetPenStartCap": [P, I],
            "GdipSetPenEndCap": [P, I],
            "GdipSetPenLineJoin": [P, I],
            "GdipDeletePen": [P],
            "GdipCreateSolidFill": [ctypes.c_uint32, PP],
            "GdipDeleteBrush": [P],
            "GdipDrawLines": [P, P, PF, I],
            "GdipDrawArc": [P, P, F, F, F, F, F, F],
            "GdipDrawEllipse": [P, P, F, F, F, F],
            "GdipFillEllipse": [P, P, F, F, F, F],
            "GdipFillPolygon2": [P, P, PF, I],
            "GdipDrawPolygon": [P, P, PF, I],
            "GdipFillRectangle": [P, P, F, F, F, F],
            "GdipDrawRectangle": [P, P, F, F, F, F],
        }
        for name, args in signatures.items():
            fn = getattr(lib, name)
            fn.argtypes = args
            fn.restype = ctypes.c_int
        token = ctypes.c_void_p()
        startup = GdiplusStartupInput(1, None, False, False)
        if lib.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None) != 0:
            raise OSError("GdiplusStartup failed")
        _gdiplus_token = token
        _gdiplus = lib
    except (OSError, AttributeError):
        _gdiplus = False
    return _gdiplus


class Canvas:
    """Shapes on a DC, in logical units. GDI+ if present, GDI if not.

    Everything here takes logical coordinates and multiplies by the display
    scale, so the layout code never sees a DPI.
    """

    UNIT_PIXEL, ANTIALIAS, CAP_ROUND, JOIN_ROUND = 2, 4, 2, 2

    def __init__(self, hdc, scale: float, force_gdi: bool = False):
        self.hdc = hdc
        self.k = scale
        self.gp = None
        self.lib = None if force_gdi else _load_gdiplus()
        if self.lib:
            graphics = ctypes.c_void_p()
            if self.lib.GdipCreateFromHDC(hdc, ctypes.byref(graphics)) == 0:
                self.gp = graphics
                self.lib.GdipSetSmoothingMode(graphics, self.ANTIALIAS)

    def close(self) -> None:
        """Release the graphics object: GDI text cannot go on the DC until then."""
        if self.gp:
            self.lib.GdipDeleteGraphics(self.gp)
            self.gp = None

    # -- pens and brushes, created per call: cheap, and nothing to leak ----

    def _pen(self, colour, width, alpha):
        pen = ctypes.c_void_p()
        self.lib.GdipCreatePen1(argb(colour, alpha), float(width * self.k),
                                self.UNIT_PIXEL, ctypes.byref(pen))
        self.lib.GdipSetPenStartCap(pen, self.CAP_ROUND)
        self.lib.GdipSetPenEndCap(pen, self.CAP_ROUND)
        self.lib.GdipSetPenLineJoin(pen, self.JOIN_ROUND)
        return pen

    def _brush(self, colour, alpha):
        brush = ctypes.c_void_p()
        self.lib.GdipCreateSolidFill(argb(colour, alpha), ctypes.byref(brush))
        return brush

    def _pts(self, points):
        k = self.k
        arr = (PointF * len(points))(*[PointF(x * k, y * k) for x, y in points])
        return arr

    # -- GDI fallback plumbing ---------------------------------------------

    def _gdi_ipts(self, points):
        k = self.k
        return (POINT * len(points))(*[POINT(int(round(x * k)), int(round(y * k)))
                                      for x, y in points])

    def _gdi_stroke(self, colour, width, draw):
        pen = gdi32.CreatePen(PS_SOLID, max(1, int(round(width * self.k))), cref(colour))
        old_pen = gdi32.SelectObject(self.hdc, pen)
        old_brush = gdi32.SelectObject(self.hdc, gdi32.GetStockObject(NULL_BRUSH))
        try:
            draw()
        finally:
            gdi32.SelectObject(self.hdc, old_brush)
            gdi32.SelectObject(self.hdc, old_pen)
            gdi32.DeleteObject(pen)

    def _gdi_fill(self, colour, draw, alpha=255):
        if alpha < 255:
            colour = tuple(int(b + (c - b) * alpha / 255) for c, b in zip(colour, BG))
        brush = gdi32.CreateSolidBrush(cref(colour))
        old_brush = gdi32.SelectObject(self.hdc, brush)
        old_pen = gdi32.SelectObject(self.hdc, gdi32.GetStockObject(NULL_PEN))
        try:
            draw()
        finally:
            gdi32.SelectObject(self.hdc, old_pen)
            gdi32.SelectObject(self.hdc, old_brush)
            gdi32.DeleteObject(brush)

    # -- the shapes ----------------------------------------------------------

    def lines(self, points, colour, width=1.0, alpha=255) -> None:
        if len(points) < 2:
            return
        if self.gp:
            pen = self._pen(colour, width, alpha)
            self.lib.GdipDrawLines(self.gp, pen, self._pts(points), len(points))
            self.lib.GdipDeletePen(pen)
            return
        pts = self._gdi_ipts(points)
        self._gdi_stroke(colour, width, lambda: gdi32.Polyline(self.hdc, pts, len(pts)))

    def polygon(self, points, colour, alpha=255, outline=None, width=1.0) -> None:
        if self.gp:
            if alpha:
                brush = self._brush(colour, alpha)
                self.lib.GdipFillPolygon2(self.gp, brush, self._pts(points), len(points))
                self.lib.GdipDeleteBrush(brush)
            if outline is not None:
                pen = self._pen(outline, width, 255)
                self.lib.GdipDrawPolygon(self.gp, pen, self._pts(points), len(points))
                self.lib.GdipDeletePen(pen)
            return
        pts = self._gdi_ipts(points)
        if alpha:
            self._gdi_fill(colour, lambda: gdi32.Polygon(self.hdc, pts, len(pts)), alpha)
        if outline is not None:
            self._gdi_stroke(outline, width,
                             lambda: gdi32.Polygon(self.hdc, pts, len(pts)))

    def circle(self, cx, cy, r, colour, alpha=255, outline=None, width=1.0) -> None:
        k = self.k
        if self.gp:
            box = ((cx - r) * k, (cy - r) * k, 2 * r * k, 2 * r * k)
            if alpha:
                brush = self._brush(colour, alpha)
                self.lib.GdipFillEllipse(self.gp, brush, *box)
                self.lib.GdipDeleteBrush(brush)
            if outline is not None:
                pen = self._pen(outline, width, 255)
                self.lib.GdipDrawEllipse(self.gp, pen, *box)
                self.lib.GdipDeletePen(pen)
            return
        l, t = int((cx - r) * k), int((cy - r) * k)
        rr, b = int((cx + r) * k), int((cy + r) * k)
        if alpha:
            self._gdi_fill(colour, lambda: gdi32.Ellipse(self.hdc, l, t, rr, b), alpha)
        if outline is not None:
            self._gdi_stroke(outline, width, lambda: gdi32.Ellipse(self.hdc, l, t, rr, b))

    def arc(self, cx, cy, r, start_deg, sweep_deg, colour, width, alpha=255) -> None:
        """An arc of a circle. Degrees clockwise from 3 o'clock, like GDI+."""
        if sweep_deg <= 0:
            return
        k = self.k
        if self.gp:
            pen = self._pen(colour, width, alpha)
            self.lib.GdipDrawArc(self.gp, pen, (cx - r) * k, (cy - r) * k,
                                 2 * r * k, 2 * r * k, float(start_deg), float(sweep_deg))
            self.lib.GdipDeletePen(pen)
            return
        # GDI's Arc takes two points on the circle, so the fallback approximates
        # the arc as a polyline, which also sidesteps its direction quirks.
        steps = max(4, int(sweep_deg / 6))
        points = []
        for i in range(steps + 1):
            a = math.radians(start_deg + sweep_deg * i / steps)
            points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        self.lines(points, colour, width)

    def rect(self, x, y, w, h, colour, alpha=255, outline=None, width=1.0) -> None:
        self.polygon([(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
                     colour, alpha, outline, width)


# -- the window ---------------------------------------------------------------

class WatchWindow:
    """The four-pillar panel. One per tray process, created on demand."""

    CLASS = "PhantomBridgeWatch"
    TITLE = "Phantom II"
    W, H = 640, 640
    DAY_MIN = 24 * 60

    def __init__(self, force_gdi: bool = False):
        self.s = 1.0                                # display scale
        self.force_gdi = force_gdi
        self._hwnd = None
        self._wndproc = WNDPROC(self._on_message)   # keep the trampoline alive
        self._fonts = {}
        self._brushes = {}
        self._class = None
        self._icon = None           # HICON on the title bar; the tray owns it
        self.on_history = None      # set by the tray: opens the history window
        self._blink = True          # phase of the at-the-limit blink
        self._blinking = False      # whether the 500 ms timer is running
        self._strip_top = None      # the bottom band; a click there opens the history

    # -- lifecycle --------------------------------------------------------

    def open(self):
        """Show the window, creating it the first time."""
        if self._hwnd and user32.IsWindow(self._hwnd):
            user32.ShowWindow(self._hwnd, SW_RESTORE)
            user32.SetForegroundWindow(self._hwnd)
            self.refresh()
            return
        hinst = kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = hinst
        cls.lpszClassName = self.CLASS
        cls.hbrBackground = self._brush(BG)
        cls.hCursor = user32.LoadCursorW(None, ctypes.c_wchar_p(32512))
        if not user32.RegisterClassExW(ctypes.byref(cls)):
            err = ctypes.get_last_error()
            if err != 1410:                       # ERROR_CLASS_ALREADY_EXISTS
                raise ctypes.WinError(err)
        self._class = cls
        self.s = system_scale()
        style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX
        sw = user32.GetSystemMetrics(0)
        # W x H is the CLIENT area -- the canvas the layout is drawn on and
        # the frame --png renders. CreateWindow takes the OUTER size, so
        # without this the caption and borders ate ~40 px off the bottom and
        # the height-relative status line landed on the absolute-y cable bar.
        rect = wintypes.RECT(0, 0, int(self.W * self.s), int(self.H * self.s))
        user32.AdjustWindowRectEx(ctypes.byref(rect), style, False, 0)
        w, h = rect.right - rect.left, rect.bottom - rect.top
        self._hwnd = user32.CreateWindowExW(
            0, self.CLASS, self.TITLE, style | WS_VISIBLE,
            max(40, sw - w - 60), 60, w, h,
            None, None, hinst, None)
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        user32.SetForegroundWindow(self._hwnd)
        if self._icon is None:
            # Standalone, or opened before the tray's first sample: a plain
            # grey glyph, so the title bar never shows the stock Windows icon.
            import tray
            self._icon = tray.make_icon(tray.GREY, None, None, size=32)
        self.set_icon(self._icon)

    def set_icon(self, hicon) -> None:
        """Put an HICON on the title bar and the taskbar. The tray calls this
        with its live state-of-charge glyph on every sample, so the window
        wears the same icon as the notification area. The caller keeps
        ownership: it must not destroy the handle until it has set another."""
        self._icon = hicon
        if hicon and self._hwnd and user32.IsWindow(self._hwnd):
            user32.SendMessageW(self._hwnd, WM_SETICON, ICON_SMALL, hicon)
            user32.SendMessageW(self._hwnd, WM_SETICON, ICON_BIG, hicon)

    def close(self):
        if self._hwnd and user32.IsWindow(self._hwnd):
            user32.DestroyWindow(self._hwnd)
        self._hwnd = None

    @property
    def is_open(self) -> bool:
        return bool(self._hwnd and user32.IsWindow(self._hwnd))

    def refresh(self):
        """Repaint, if anyone is looking. A closed window costs nothing."""
        if self.is_open:
            user32.InvalidateRect(self._hwnd, None, False)

    # -- GDI resources, cached for the process lifetime --------------------

    def _brush(self, colour):
        key = cref(colour)
        if key not in self._brushes:
            self._brushes[key] = gdi32.CreateSolidBrush(key)
        return self._brushes[key]

    def _font(self, size, weight=400, face=UI_FACE):
        key = (round(size * self.s), weight, face)
        if key not in self._fonts:
            self._fonts[key] = gdi32.CreateFontW(
                -key[0], 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, face)
        return self._fonts[key]

    def _text(self, hdc, text, x, y, w, h, colour, size, weight=400,
              align=DT_LEFT, face=UI_FACE):
        gdi32.SelectObject(hdc, self._font(size, weight, face))
        gdi32.SetTextColor(hdc, cref(colour))
        k = self.s
        rect = wintypes.RECT(int(x * k), int(y * k), int((x + w) * k), int((y + h) * k))
        user32.DrawTextW(hdc, text, -1, ctypes.byref(rect),
                         align | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX)

    def _mono(self, hdc, text, x, y, w, h, colour=INK_DIM, size=11, align=DT_CENTER):
        self._text(hdc, text, x, y, w, h, colour, size, 400, align, MONO_FACE)

    def _fill(self, hdc, x, y, w, h, colour):
        if w <= 0 or h <= 0:
            return
        k = self.s
        rect = wintypes.RECT(int(x * k), int(y * k), int((x + w) * k), int((y + h) * k))
        user32.FillRect(hdc, ctypes.byref(rect), self._brush(colour))

    # -- pieces of the picture ------------------------------------------------

    @staticmethod
    def stress_colour(base, stress, warn_at):
        """The colour a leg wears for how close it is to its limit."""
        if stress is None:
            return base
        if stress >= 1.0:
            return BAD
        if stress >= warn_at:
            return WARN
        return base

    def _arrow(self, cv: Canvas, x1, y1, x2, y2, colour, watts, scale,
               stress=None, warn_at=0.8) -> None:
        """One leg of the balance. Thickness is watts on the shared scale;
        a leg carrying nothing is a faint hairline with no head. Colour is
        the leg's stress against its own limit: amber near it, red at it,
        and at it the arrow blinks."""
        live = watts > 1
        frac = min(1.0, watts / scale) if live else 0.0
        width = 1.5 + 9.0 * frac
        colour = self.stress_colour(colour, stress, warn_at) if live else INK_FAINT
        if live and stress is not None and stress >= 1.0 and not self._blink:
            colour = tuple(int(b + (c - b) * 0.35) for c, b in zip(colour, BG))
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        if not live:
            cv.lines([(x1, y1), (x2, y2)], colour, 1.0, alpha=140)
            return
        head_len = 9 + width * 1.3
        head_w = 5 + width * 0.9
        bx, by = x2 - ux * head_len, y2 - uy * head_len      # base of the head
        cv.lines([(x1, y1), (bx, by)], colour, width)
        px, py = -uy, ux                                     # perpendicular
        cv.polygon([(x2, y2), (bx + px * head_w, by + py * head_w),
                    (bx - px * head_w, by - py * head_w)], colour)

    @staticmethod
    def _icon_solar(cv: Canvas, cx, y, live: bool) -> None:
        colour = SOLAR if live else INK_FAINT
        # a small sun, up and to the left of the panel
        sx, sy = cx - 24, y + 9
        cv.circle(sx, sy, 5.5, colour, 255)
        for i in range(8):
            a = math.radians(i * 45)
            cv.lines([(sx + math.cos(a) * 8.5, sy + math.sin(a) * 8.5),
                      (sx + math.cos(a) * 12, sy + math.sin(a) * 12)], colour, 1.6)
        # the panel: a tilted slab, three cells by two
        top, bot = y + 14, y + 36
        panel = [(cx - 6, top), (cx + 26, top), (cx + 32, bot), (cx - 12, bot)]
        cv.polygon(panel, colour, alpha=60, outline=colour, width=1.4)
        for f in (1 / 3, 2 / 3):
            cv.lines([(cx - 6 + 32 * f, top), (cx - 12 + 44 * f, bot)], colour, 1.0, 180)
        mid = (top + bot) / 2
        cv.lines([(cx - 9, mid), (cx + 29, mid)], colour, 1.0, 180)

    @staticmethod
    def _icon_grid(cv: Canvas, cx, y, live: bool) -> None:
        colour = GRID if live else INK_FAINT
        base = y + 40
        cv.lines([(cx - 13, base), (cx - 4, y)], colour, 1.8)
        cv.lines([(cx + 13, base), (cx + 4, y)], colour, 1.8)
        cv.lines([(cx - 18, y + 8), (cx + 18, y + 8)], colour, 1.8)     # top arm
        cv.lines([(cx - 13, y + 19), (cx + 13, y + 19)], colour, 1.8)   # lower arm
        cv.lines([(cx - 8, y + 24), (cx + 10, base - 4)], colour, 1.0, 200)
        cv.lines([(cx + 8, y + 24), (cx - 10, base - 4)], colour, 1.0, 200)
        cv.lines([(cx - 15, y + 8), (cx - 15, y + 13)], colour, 1.2)     # insulators
        cv.lines([(cx + 15, y + 8), (cx + 15, y + 13)], colour, 1.2)

    @staticmethod
    def _icon_home(cv: Canvas, cx, y, live: bool) -> None:
        colour = LOAD if live else INK_FAINT
        roof = [(cx - 21, y + 17), (cx, y), (cx + 21, y + 17)]
        cv.lines(roof, colour, 2.0)
        cv.rect(cx - 15, y + 17, 30, 23, colour, alpha=50, outline=colour, width=1.4)
        cv.rect(cx - 4, y + 27, 8, 13, colour, alpha=255)               # door
        cv.rect(cx + 6, y + 22, 6, 6, colour, alpha=255)                # window

    @staticmethod
    def _icon_inverter(cv: Canvas, cx, cy, w, h) -> None:
        cv.rect(cx - w / 2, cy - h / 2, w, h, PANEL, alpha=255, outline=LINE, width=1.2)
        # a little sine on the face: it is the thing that makes AC
        pts = []
        for i in range(25):
            t = i / 24
            pts.append((cx - 22 + 44 * t, cy + 22 + math.sin(t * math.pi * 2) * 4))
        cv.lines(pts, INK_FAINT, 1.2)

    @staticmethod
    def _gauge(cv: Canvas, cx, cy, r, soc, state_colour) -> None:
        """The battery as a tank: a liquid level inside a circle.

        The empty part is drawn, not implied -- the tank body is a shade
        lighter than the window with a rim, so 40% reads as "a tank that is
        60% empty" and not as a ring that stops early. The surface is a low
        wave, two layers with different phases for a little depth; the phase
        moves with the clock, so each new sample shifts it slightly.
        """
        cv.circle(cx, cy, r, PANEL, 255, outline=LINE, width=1.5)
        if soc is None:
            return
        frac = max(0.0, min(1.0, soc / 100.0))
        if frac <= 0:
            return
        level = cy + r - 2 * r * frac
        steps = 48
        base = time.time() * 0.7
        for amp, phase, alpha in ((2.4, base, 215), (1.7, base + 2.1, 95)):
            xs = [cx - r + 2 * r * i / steps for i in range(steps + 1)]
            top_edge, bottom_edge = [], []
            for x in xs:
                inside = r * r - (x - cx) ** 2
                half = math.sqrt(inside) if inside > 0 else 0.0
                wave = level + amp * math.sin((x - cx) / (r * 0.42) + phase)
                wave = min(max(wave, cy - half), cy + half)
                top_edge.append((x, wave))
                bottom_edge.append((x, max(cy + half, wave)))
            cv.polygon(top_edge + bottom_edge[::-1], state_colour, alpha)
        # a thin bright meniscus where the liquid meets the tank
        cv.lines(top_edge[1:-1], INK, 1.0, alpha=70) if frac < 0.99 else None

    # -- the 24-hour rule: the day's lanes, and where the plan cuts them -------
    #
    # One horizontal rule under the board, running **sunrise to sunrise**
    # (owner, 2026-09-22) -- from the turnaround, where the sun starts
    # lifting the pack, to the next one. That is the plan's own day: the
    # night is one stretch instead of being sawn in half at midnight, and
    # both cuts fall on the scale instead of off the right edge.
    #
    # Left of the now marker it is the record -- what the inverter was
    # actually set to, from the tape the collector publishes -- and right of
    # it the plan, the same wash in a lighter key. The two cuts a day are
    # the whole point of it: SBU -> SUB at dusk, when the pack becomes the
    # outage reserve, and SUB -> SBU at the release. A time the plan has not
    # worked out yet (tonight's release, before the hold begins) wears a
    # "~"; a hold with no clock at all (the floor) fades out instead of
    # claiming one. Green is the pack carrying the house, blue the grid,
    # exactly as the pillars above use them.

    def _lane_x(self, x0, w, minute, origin=0):
        into = max(0.0, min(float(self.DAY_MIN), float(minute) - origin))
        return x0 + w * into / self.DAY_MIN

    @staticmethod
    def _first_after(minute, origin):
        """The first occurrence of a time of day at or after `origin`."""
        m = int(minute)
        while m < origin:
            m += 24 * 60
        while m - 24 * 60 >= origin:
            m -= 24 * 60
        return m

    @staticmethod
    def _lane_colour(want, grid=None, src=None):
        """Whose power the house was on over that stretch -- the setting
        says who was *meant* to carry it, the source says who did. SBU is
        green while the sun carries it and teal when the pack does; SUB is
        orange on the utility and blue when the grid failed and the pack
        carried it anyway. With no source (the plan ahead of now) the
        setting's own colour stands in."""
        if src == "solar":
            return SOLAR
        if src == "grid":
            return GRID
        if src == "battery":
            return LANE_OUTAGE if want == "SUB" else BATT
        if want == "SBU":
            return BATT
        if want == "SUB":
            return LANE_OUTAGE if grid is False else GRID
        return INK_FAINT

    @staticmethod
    def _lane_cuts(past, ahead):
        """The switch points worth a mark: where the setting actually
        changed today, and where the plan says it will change next."""
        cuts, last = [], None
        for lane in past:
            shown = lane.get("actual") or lane.get("want")
            if shown and shown != last:
                if last is not None:
                    cuts.append({"at": lane["from"], "ahead": False, "sure": True})
                last = shown
        for i, lane in enumerate(ahead):
            if i == 0:
                continue                      # that edge is the now marker
            prev = ahead[i - 1]
            if prev.get("open"):
                continue                      # nothing says when that one ends
            cuts.append({"at": lane["from"], "ahead": True, "sure": bool(prev.get("sure"))})
        return cuts

    def _lanes_shapes(self, cv, x0, y, w, h, past, ahead, now_min, sun=None,
                      origin=0) -> None:
        cv.rect(x0, y, w, h, PANEL, alpha=235, outline=LINE, width=1.0)
        if sun and sun[0] is not None and sun[1] is not None:
            rise = self._first_after(sun[0], origin)
            a = self._lane_x(x0, w, rise, origin)
            b = self._lane_x(x0, w, self._first_after(sun[1], rise), origin)
            if b > a:
                cv.rect(a, y, b - a, h, SOLAR, alpha=22)     # the sun's window
        for lane in past:
            shown = lane.get("actual") or lane.get("want")
            x1 = self._lane_x(x0, w, lane["from"], origin)
            x2 = self._lane_x(x0, w, lane["to"], origin)
            if not shown or x2 <= x1:
                continue
            cv.rect(x1, y, x2 - x1, h,
                    self._lane_colour(shown, lane.get("grid"), lane.get("src")), alpha=210)
            if lane.get("actual") and lane.get("want") != lane["actual"]:
                # the plan wanted the other one: the inverter's own timer
                # (menu 99), a hand on the panel, or the autopilot advisory
                cv.rect(x1, y + h - 3, x2 - x1, 3, BAD, alpha=255)
        for lane in ahead:
            x1 = self._lane_x(x0, w, lane["from"], origin)
            x2 = self._lane_x(x0, w, lane["to"], origin)
            if x2 <= x1:
                continue
            colour = self._lane_colour(lane["want"])
            alpha = 80 if lane.get("sure") else 48
            if lane.get("open"):
                steps = 10
                for i in range(steps):
                    cv.rect(x1 + (x2 - x1) * i / steps, y, (x2 - x1) / steps + 0.6, h,
                            colour, alpha=int(alpha * (1 - i / steps)))
            else:
                cv.rect(x1, y, x2 - x1, h, colour, alpha=alpha)
        for cut in self._lane_cuts(past, ahead):
            x = self._lane_x(x0, w, cut["at"], origin)
            cv.lines([(x, y - 3), (x, y + h + 3)],
                     INK if cut["ahead"] and cut["sure"] else INK_DIM, 1.2)
        xn = self._lane_x(x0, w, now_min, origin)
        cv.lines([(xn, y - 6), (xn, y + h + 4)], INK, 1.4)
        cv.polygon([(xn - 4, y - 11), (xn + 4, y - 11), (xn, y - 5)], INK)

    def _lanes_text(self, hdc, x0, y, w, h, past, ahead, now_min, origin=0) -> None:
        label_y = y + h + 3
        for lane in past:
            shown = lane.get("actual") or lane.get("want")
            x1 = self._lane_x(x0, w, lane["from"], origin)
            x2 = self._lane_x(x0, w, lane["to"], origin)
            if shown and x2 - x1 >= 32:
                self._text(hdc, shown, x1, y + 1, x2 - x1, h - 2, BG, 9, 700, DT_CENTER)
        for lane in ahead:
            x1 = self._lane_x(x0, w, lane["from"], origin)
            x2 = self._lane_x(x0, w, lane["to"], origin)
            if x2 - x1 >= 32:
                self._text(hdc, lane["want"], x1, y + 1, x2 - x1, h - 2,
                           INK_DIM if lane.get("sure") else INK_FAINT, 9, 700, DT_CENTER)
        # The times, in the order they matter: where the plan cuts next,
        # then where the day already turned, then whatever hours are left
        # over. A label is dropped rather than drawn over its neighbour.
        taken = [self._lane_x(x0, w, now_min, origin)]
        tail = ahead[-1] if ahead else None
        if tail and tail["to"] > origin + self.DAY_MIN and not tail.get("open"):
            # past the next sunrise, off the end of the scale
            self._mono(hdc, ("" if tail.get("sure") else "~") + policy.fmt_hm(tail["to"]) + " >",
                       x0 + w - 62, label_y, 62, 14,
                       INK if tail.get("sure") else INK_DIM, 10, DT_RIGHT)
            taken.append(x0 + w - 30)
        cuts = self._lane_cuts(past, ahead)
        for cut in sorted(cuts, key=lambda c: not c["ahead"]):
            x = self._lane_x(x0, w, cut["at"], origin)
            if any(abs(x - other) < 26 for other in taken):
                continue
            taken.append(x)
            colour = (INK if cut["sure"] else INK_DIM) if cut["ahead"] else INK_FAINT
            self._mono(hdc, ("" if cut["sure"] else "~") + policy.fmt_hm(cut["at"]),
                       x - 26, label_y, 52, 14, colour, 10)
        # the round hours that fall inside the window, wherever they land
        for hour in (0, 6, 12, 18):
            at = self._first_after(hour * 60, origin)
            x = self._lane_x(x0, w, at, origin)
            if any(abs(x - other) < 30 for other in taken) or x >= x0 + w - 12:
                continue
            self._mono(hdc, "%02d" % hour, x - 14, label_y, 28, 14, INK_FAINT, 9)

    # -- painting ---------------------------------------------------------

    def _paint(self, hdc, dev_w, dev_h):
        width, height = dev_w / self.s, dev_h / self.s
        state = bridge.read_state(max_age_s=10 ** 9) or {}
        flow = state.get("flow") or {}
        today = state.get("today") or {}
        loan = bridge.handover_remaining()
        age = state.get("age_s", 9999)
        fresh = bool(state) and age < 30

        self._fill(hdc, 0, 0, width, height, BG)
        pad = 20

        # -- header: who is carrying the house right now, in one phrase.
        # QMOD's letter is NOT that answer (B is inverter mode, which is
        # where the unit sits all day on solar); flow.source is.
        self._text(hdc, "Phantom II", pad, 12, 240, 26, INK, 17, 600)
        self._text(hdc, "6 kVA hybrid  -  local link", pad, 36, 300, 16, INK_FAINT, 10)
        source = flow.get("source")
        label = flow.get("source_label") or "NO DATA"
        colour = SOURCE_COLOURS.get(source, INK_FAINT)
        if source == "battery" and not flow.get("grid_present", True):
            colour = BAD
        if loan > 0:
            label, colour = "LENT TO APP", WARN
        elif not fresh:
            label, colour = "STALE", INK_FAINT
        self._text(hdc, label, width - pad - 240, 12, 240, 26, colour, 12, 700, DT_RIGHT)
        clock = (state.get("local") or "")[11:19] or "--:--:--"
        sub = clock + ("  live" if age < 8 else f"  {age}s ago" if fresh else "  no fresh sample")
        self._mono(hdc, sub, width - pad - 240, 36, 240, 16, INK_FAINT, 11, DT_RIGHT)

        if not state:
            self._text(hdc, "No collector running.", pad, height / 2 - 30,
                       width - pad * 2, 24, INK, 14, 600, DT_CENTER)
            self._text(hdc, "Start it with:  python collector.py", pad,
                       height / 2, width - pad * 2, 22, INK_DIM, 12, 400, DT_CENTER)
            return
        if loan > 0:
            self._text(hdc, f"Lent to the WatchPower app - {loan / 60:.0f} min left",
                       pad, height / 2 - 20, width - pad * 2, 24, WARN, 13, 600, DT_CENTER)
            self._text(hdc, "Readings resume automatically.", pad, height / 2 + 6,
                       width - pad * 2, 22, INK_DIM, 11, 400, DT_CENTER)
            return

        # -- the numbers
        charge = max(0.0, flow.get("batt_w") or 0.0)
        discharge = max(0.0, -(flow.get("batt_w") or 0.0))
        pv = flow.get("pv_w") or 0.0
        grid = flow.get("grid_w") or 0.0
        load = flow.get("load_w") or 0.0
        scale = max(pv, grid, discharge, load, charge, 1.0)
        soc = state.get("soc")
        batt_state = "discharging" if discharge else "charging" if charge else "idle"
        batt_colour = (WARN if discharge else BATT if charge else INK_DIM)
        if discharge and soc is not None and soc < 20:
            batt_colour = BAD

        # -- geometry: four pillars around the inverter, on a shared axis.
        # The side pillars sit high enough that their third line of small
        # figures clears the horizontal arrows; the old layout ran them into
        # each other.
        cx = width / 2
        col_w = 200
        gx, hx = pad + col_w / 2 - 10, width - pad - col_w / 2 + 10     # grid / home centres
        box_w, box_h = 144, 66
        box_y = 286                                                   # inverter centre
        solar_top, batt_cy, batt_r = 58, 410, 38
        side_icon_y = box_y - 140

        # The day's figures and the cable bar used to sit under the board.
        # They live in the history window now (the arrows carry the limits),
        # so this view is the four dimensions, the day's rule and one line
        # of status. A click anywhere in that bottom band opens the history.
        rule_x, rule_w, rule_y, rule_h = pad, width - pad * 2, height - 78, 16
        self._strip_top = rule_y - 12

        # the rule's own clock is the wall clock, not the sample's: the plan
        # is about the time of day, and a stale link must not freeze it
        site_now = bridge.now_site()
        now_min = site_now.hour * 60 + site_now.minute + site_now.second / 60.0
        plan = state.get("policy") or {}
        turnaround = policy.parse_hm(plan.get("turnaround"))
        # sunrise to sunrise, so the night is one stretch and both cuts are
        # on the scale; midnight to midnight only if there is no turnaround
        rule_origin = policy.window_origin(turnaround, now_min) if turnaround is not None else 0
        past = policy.lanes_behind((plan.get("tape") or {}).get("marks"), now_min,
                                   site_now.date(), since=rule_origin)
        ahead = policy.lanes_from(plan, now_min)
        sun = (turnaround, policy.parse_hm(plan.get("dusk")))

        # -- each leg against its own limit (see config, "Load side"); the
        # tray icon judges from the same function, so they never disagree
        grid_v = flow.get("grid_v") or 0.0
        grid_a = grid / grid_v if grid_v and grid > 1 else 0.0
        batt_a = (flow.get("batt_discharge_a") if discharge else flow.get("batt_charge_a")) or 0.0
        stress = flowmod.leg_stress(flow, config)
        limits = {leg: (stress[leg] or (None, 0.8)) for leg in ("grid", "home", "battery")}
        at_limit = flowmod.at_limit(stress)
        self._set_blinking(bool(at_limit))

        cv = Canvas(hdc, self.s, self.force_gdi)
        try:
            # legs first, so the pillars sit on top of them
            self._arrow(cv, cx, 176, cx, box_y - box_h / 2 - 6, SOLAR, pv, scale)
            self._arrow(cv, gx + 74, box_y, cx - box_w / 2 - 6, box_y, GRID, grid, scale,
                        *limits["grid"])
            self._arrow(cv, cx + box_w / 2 + 6, box_y, hx - 74, box_y, LOAD, load, scale,
                        *limits["home"])
            if charge:
                self._arrow(cv, cx, box_y + box_h / 2 + 6, cx, batt_cy - batt_r - 20,
                            BATT, charge, scale, *limits["battery"])
            else:
                self._arrow(cv, cx, batt_cy - batt_r - 20, cx, box_y + box_h / 2 + 6,
                            WARN if discharge else INK_FAINT, discharge, scale,
                            *limits["battery"])

            self._icon_inverter(cv, cx, box_y, box_w, box_h)
            self._icon_solar(cv, cx, solar_top, pv > 1)
            self._icon_grid(cv, gx, side_icon_y, grid > 1 or flow.get("grid_present", False))
            self._icon_home(cv, hx, side_icon_y, load > 1)
            self._gauge(cv, cx, batt_cy, batt_r, soc, batt_colour)
            self._lanes_shapes(cv, rule_x, rule_y, rule_w, rule_h, past, ahead, now_min,
                               sun, rule_origin)
            cv.rect(pad, height - 40, width - pad * 2, 1, LINE)
        finally:
            cv.close()

        # -- text on top of the shapes
        # solar
        self._text(hdc, "SOLAR", cx - 100, solar_top + 40, 200, 16, INK_DIM, 9, 700, DT_CENTER)
        self._text(hdc, _watts(pv), cx - 100, solar_top + 56, 200, 30,
                   SOLAR if pv > 1 else INK_FAINT, 22, 700, DT_CENTER)
        self._mono(hdc, f"{flow.get('pv_v') or 0:.1f} V  {flow.get('pv_a') or 0:.1f} A",
                   cx - 100, solar_top + 88, 200, 18)

        # grid
        gy = side_icon_y + 44
        self._text(hdc, "GRID", gx - 100, gy, 200, 16, INK_DIM, 9, 700, DT_CENTER)
        self._text(hdc, _watts(grid), gx - 100, gy + 16, 200, 30,
                   self.stress_colour(GRID, *limits["grid"]) if grid > 1 else INK_FAINT,
                   22, 700, DT_CENTER)
        if flow.get("grid_present"):
            grid_line = f"{grid_v:.1f} V  {flow.get('grid_hz') or 0:.1f} Hz"
            if grid_a:
                grid_line += f"  {grid_a:.1f} A"
            grid_note = ("bypassed to the house" if flow.get("mode") == "L"
                         else "present, on standby" if grid <= 1 else "derived")
        else:
            grid_line, grid_note = "no grid", "outage"
        self._mono(hdc, grid_line, gx - 100, gy + 48, 200, 18,
                   INK_DIM if flow.get("grid_present") else BAD)
        self._mono(hdc, grid_note, gx - 100, gy + 66, 200, 16,
                   INK_FAINT if flow.get("grid_present") else BAD, 10)

        # home
        self._text(hdc, "HOME", hx - 100, gy, 200, 16, INK_DIM, 9, 700, DT_CENTER)
        self._text(hdc, _watts(load), hx - 100, gy + 16, 200, 30,
                   self.stress_colour(LOAD, *limits["home"]) if load > 1 else INK_FAINT,
                   22, 700, DT_CENTER)
        bits = []
        if flow.get("out_v"):
            bits.append(f"{flow['out_v']:.0f} V  {flow.get('out_hz') or 0:.1f} Hz")
        if flow.get("load_a") is not None:
            bits.append(f"{flow['load_a']:.1f} A")
        self._mono(hdc, "  ".join(bits), hx - 100, gy + 48, 200, 18)
        bits = []
        if flow.get("load_va"):
            bits.append(f"{flow['load_va']:,.0f} VA")
        if flow.get("power_factor"):
            bits.append(f"pf {flow['power_factor']:.2f}")
        if flow.get("load_pct") is not None:
            bits.append(f"{flow['load_pct']}%")
        self._mono(hdc, "  ".join(bits), hx - 100, gy + 66, 200, 16, INK_FAINT, 10)

        # inverter: the stage that is making the AC, in the unit's own terms
        self._text(hdc, "PHANTOM II", cx - box_w / 2, box_y - 28, box_w, 16, INK_DIM, 9, 700, DT_CENTER)
        mode = flow.get("mode")
        mode_word = (flow.get("mode_word") or "?").upper()
        mode_colour = (BAD if mode == "F" else GRID if mode == "L"
                       else SOLAR if source == "solar" else WARN if mode == "B" else INK_DIM)
        self._text(hdc, mode_word, cx - box_w / 2, box_y - 12, box_w, 16, mode_colour, 10, 700, DT_CENTER)
        self._mono(hdc, f"mode {mode or '?'}", cx - box_w / 2, box_y + 4, box_w, 14, INK_FAINT, 9)

        # battery
        self._text(hdc, "--" if soc is None else str(soc), cx - 39, batt_cy - 15, 80, 30,
                   BG, 22, 700, DT_CENTER)
        self._text(hdc, "--" if soc is None else str(soc), cx - 40, batt_cy - 16, 80, 30,
                   INK, 22, 700, DT_CENTER)
        self._text(hdc, "%" if soc is not None else "", cx - 40, batt_cy + 12, 80, 14,
                   INK_FAINT, 9, 400, DT_CENTER)
        by = batt_cy + batt_r + 8
        self._text(hdc, "BATTERY", cx - 100, by, 200, 16, INK_DIM, 9, 700, DT_CENTER)
        batt_w = flow.get("batt_w") or 0.0
        self._text(hdc, ("-" if batt_w < 0 else "+" if batt_w > 0 else "")
                   + _watts(abs(batt_w)), cx - 100, by + 16, 200, 30,
                   self.stress_colour(batt_colour, *limits["battery"]) if abs(batt_w) > 1
                   else INK_FAINT, 22, 700, DT_CENTER)
        self._mono(hdc, f"{flow.get('batt_v') or 0:.2f} V  {batt_a:.0f} A of "
                        f"{config.BATTERY_MAX_A:.0f}  {batt_state}",
                   cx - 150, by + 48, 300, 18,
                   self.stress_colour(INK_DIM, *limits["battery"]))
        if not state.get("trust_soc", False):
            self._mono(hdc, "% is the inverter's voltage estimate",
                       cx - 150, by + 66, 300, 16, INK_FAINT, 10)

        # -- the day's rule, and under it the status line and the way in
        self._lanes_text(hdc, rule_x, rule_y, rule_w, rule_h, past, ahead, now_min,
                         rule_origin)
        if not (past or ahead):
            self._text(hdc, "no plan published", rule_x, rule_y + 1, rule_w, rule_h - 2,
                       INK_FAINT, 9, 400, DT_CENTER)
        sy = height - 28
        self._mono(hdc, "history >", width - pad - 90, sy, 90, 18, INK_FAINT, 10, DT_RIGHT)
        warns = state.get("warnings") or []
        if at_limit:
            names = {"grid": "GRID LINE", "home": "HOME LINES", "battery": "BATTERY"}
            self._text(hdc, " and ".join(names[n] for n in at_limit) + " AT THE LIMIT",
                       pad, sy, width - pad * 2 - 90, 18, BAD if self._blink else WARN, 11, 700)
        elif warns:
            self._text(hdc, "WARNING: " + ", ".join(warns).replace("_", " "),
                       pad, sy, width - pad * 2 - 90, 18, BAD, 11, 700)
        elif source == "battery" and not flow.get("grid_present", True):
            self._text(hdc, f"GRID OUT - {_watts(load)} from the pack",
                       pad, sy, width - pad * 2 - 90, 18, WARN, 11, 700)
        elif policy.headline(state.get("policy")):
            plan = state["policy"]
            line = policy.headline(plan)
            if plan.get("phase") == "hold" and plan.get("release_at"):
                line += "  -  grid carries the house, the pack is the reserve"
            elif plan.get("phase") == "night":
                line += " (~%s)" % plan.get("turnaround")
            elif plan.get("phase") == "floor":
                line += " past %s%%" % plan.get("resume")
            self._text(hdc, line, pad, sy, width - pad * 2 - 90, 18,
                       INK_FAINT if plan.get("want") == plan.get("current") or not plan.get("enabled")
                       else WARN, 11)
        elif source in ("battery", "solar+battery"):
            self._text(hdc, "Drawing on the pack by priority (SBU) - the grid is there and unused",
                       pad, sy, width - pad * 2 - 90, 18, INK_FAINT, 11)
        elif source == "solar":
            self._text(hdc, "The sun is carrying the house" + (
                " and charging the pack" if charge > 1 else ""),
                       pad, sy, width - pad * 2 - 90, 18, INK_FAINT, 11)
        elif source == "grid":
            self._text(hdc, "Grid bypass - the house is on the utility",
                       pad, sy, width - pad * 2 - 90, 18, INK_FAINT, 11)

    # -- the blink ------------------------------------------------------------

    def _set_blinking(self, wanted: bool) -> None:
        """Start or stop the 500 ms timer. It exists only while a leg is at
        its limit, so a calm window still costs nothing between samples."""
        if not (self._hwnd and user32.IsWindow(self._hwnd)):
            self._blink = True
            return
        if wanted and not self._blinking:
            self._blinking = bool(user32.SetTimer(self._hwnd, BLINK_TIMER, 500, None))
        elif not wanted and self._blinking:
            user32.KillTimer(self._hwnd, BLINK_TIMER)
            self._blinking, self._blink = False, True

    # -- input --------------------------------------------------------------

    def _on_input(self, msg, wparam, lparam) -> bool:
        """Keys and clicks. True if handled. The day strip opens the history."""
        if msg == WM_LBUTTONDOWN and self.on_history and self._strip_top is not None:
            y = ((lparam >> 16) & 0xFFFF) / self.s
            if y >= self._strip_top:
                self.on_history()
                return True
        return False

    # -- a frame to a file, for looking at it without a screen -----------------

    def render_png(self, path: str, scale: float | None = None) -> None:
        """Paint one frame into a DIB and write it as a PNG. Test aid."""
        self.s = scale or system_scale()
        w, h = int(self.W * self.s), int(self.H * self.s)
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = w, -h
        bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = gdi32.SelectObject(mem, bmp)
        gdi32.SetBkMode(mem, TRANSPARENT)
        try:
            self._paint(mem, w, h)
            gdi32.GdiFlush()
            raw = ctypes.string_at(bits, w * h * 4)
        finally:
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)
        rows = b"".join(
            b"\x00" + bytes(v for x in range(w)
                            for v in (raw[(y * w + x) * 4 + 2], raw[(y * w + x) * 4 + 1],
                                      raw[(y * w + x) * 4]))
            for y in range(h))

        def chunk(kind, data):
            body = kind + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))
        pathlib.Path(path).write_bytes(png)

    # -- messages ---------------------------------------------------------

    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_ERASEBKGND:
            return 1                       # we paint every pixel; skip the flicker
        if msg == WM_PAINT:
            ps = PAINTSTRUCT()
            hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
            rect = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            w, h = rect.right, rect.bottom
            # Double buffered: compose off-screen, blit once. Painting straight
            # to the window DC tears visibly on a panel this dense.
            mem = gdi32.CreateCompatibleDC(hdc)
            bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
            old = gdi32.SelectObject(mem, bmp)
            gdi32.SetBkMode(mem, TRANSPARENT)
            try:
                try:
                    self._paint(mem, w, h)
                except Exception as exc:      # a bad sample must not kill the tray
                    self._fill(mem, 0, 0, w / self.s, h / self.s, BG)
                    self._text(mem, f"paint error: {exc!r}"[:120], 20, 20, w / self.s - 40,
                               20, BAD, 10)
                gdi32.BitBlt(hdc, 0, 0, w, h, mem, 0, 0, SRCCOPY)
            finally:
                gdi32.SelectObject(mem, old)
                gdi32.DeleteObject(bmp)
                gdi32.DeleteDC(mem)
            user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg == WM_APP_SAMPLE:
            user32.InvalidateRect(hwnd, None, False)
            return 0
        if msg == WM_TIMER and wparam == BLINK_TIMER:
            self._blink = not self._blink
            user32.InvalidateRect(hwnd, None, False)
            return 0
        if self._on_input(msg, wparam, lparam):
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            self._hwnd = None
            self._blinking, self._blink = False, True
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


if __name__ == "__main__":
    # Standalone: useful for looking at the panel without the tray.
    import argparse
    parser = argparse.ArgumentParser(description="the watch screen, on its own")
    parser.add_argument("--png", metavar="PATH", help="render one frame to a PNG and exit")
    parser.add_argument("--gdi", action="store_true", help="force the plain-GDI fallback")
    parser.add_argument("--scale", type=float, help="display scale for --png (default: system)")
    args = parser.parse_args()
    set_dpi_aware()
    win = WatchWindow(force_gdi=args.gdi)
    if args.png:
        win.render_png(args.png, args.scale)
        print(f"wrote {args.png}")
        raise SystemExit(0)
    win.open()
    msg = wintypes.MSG()
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
