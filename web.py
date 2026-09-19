#!/usr/bin/env python3
"""The watch screen. A local dashboard that shows every source and every load
at once, which is the thing WatchPower will not do.

    python web.py                 serve on 0.0.0.0:8080
    python web.py --port 9000

Open it on this PC at http://localhost:8080, or from a phone on the same WiFi
at the LAN address printed on startup. That is the point: it replaces the
vendor app's screen with one that tells the truth, on the same device you
would have reached for the app on.

It reads `logs/state.json` and nothing else. No dongle session, no RS-485
traffic, no contention with the collector or the tray -- so it costs the
inverter exactly nothing and can run all day. If the collector is not running,
the page says so rather than showing a stale number.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

import bridge
import config
import netutil

AUTOSTART_VALUE = "PhantomBridgeWeb"

HERE = pathlib.Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"


def payload() -> dict:
    """Everything the page needs, in one object."""
    state = bridge.read_state(max_age_s=10 ** 9) or {}
    loan = bridge.handover_remaining()
    return {
        "ok": bool(state),
        "state": state,
        "handover_s": round(loan),
        "trust_soc": config.TRUST_SOC,
        "capacity_ah": config.BATTERY_CAPACITY_AH,
        "site_tz": config.SITE_TZ,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body: bytes, content_type: str, cache: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                body = PAGE.read_bytes()
            except OSError:
                self.send_error(500, "web/index.html is missing")
                return
            self._send(body, "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(json.dumps(payload()).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self.send_error(404)

    def log_message(self, fmt, *args) -> None:
        """Quiet by default -- a page polling every 2 s would flood the console."""
        return


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--autostart", choices=("on", "off"),
                        help="serve the watch screen at login, and exit")
    args = parser.parse_args()

    if args.autostart:
        extra = ["--port", str(args.port)] if args.port != 8080 else []
        bridge.set_autostart(AUTOSTART_VALUE, args.autostart == "on",
                             "web.py", extra)
        print(f"Watch screen autostart "
              f"{'enabled' if args.autostart == 'on' else 'disabled'}.")
        return 0

    bridge.refresh_autostart(AUTOSTART_VALUE, "web.py")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    lan = netutil.primary_lan_ip() or "this-machine"
    print("phantom-bridge watch screen")
    print(f"  this PC : http://localhost:{args.port}")
    print(f"  phone   : http://{lan}:{args.port}   (same WiFi)")
    if bridge.read_state(max_age_s=60) is None:
        print("\n  NOTE: no fresh data. Start the collector:")
        print("        python collector.py")
    print(f"\n  If the phone cannot reach it, allow the port once, elevated:")
    print("        " + netutil.firewall_hint(args.port))
    print("\nCtrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
