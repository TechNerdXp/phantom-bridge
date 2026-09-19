#!/usr/bin/env python3
"""Step 1 -- dongle recon. Read-only: it listens and port-scans, changes nothing.

    python probe.py                  # sweep every local broadcast domain
    python probe.py 192.168.0.42     # unicast a known IP

Answers three questions, in order of how much they matter:

  1. Is the dongle there, and on what IP?
  2. Does it expose a TCP port we can connect *out* to? If yes, we never have
     to redirect it, WatchPower keeps working, and there is nothing to undo.
  3. Does it answer the `set>...;` config channel? That is the redirect path.

Nothing here mutates the dongle. `set>...=?;` is a query form; if this
firmware treats an unknown query as a write, it is writing the value it
already has.
"""
from __future__ import annotations

import socket
import sys
import time

sys.path.insert(0, "src")

import config
import netutil

# Config-channel ports worth trying. 58899 is the Eybond/Shinemonitor
# convention; 48899 is the Solarman/IGEN one, cheap to rule out.
UDP_PORTS = (config.UDP_CONFIG_PORT, 48899)

PROBES = [
    b"set>server=?;",
    b"set>wifi_ssid=?;",
    b"set>sn=?;",
    b"AT+\n",
    b"WIFIKIT-214028-READ",       # Solarman-family magic string
    config.DONGLE_PN.encode(),
]

TCP_PORTS = (8899, 502, 18899, 80, 23, 8000, 58899)


def udp_sweep(targets, port: int, timeout: float = 4.0) -> dict:
    """Fire every probe at every target on one port, collect whatever answers."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    for target in targets:
        for payload in PROBES:
            try:
                sock.sendto(payload, (target, port))
            except OSError as exc:
                print(f"  send to {target}:{port} failed: {exc}")
    seen: dict[str, list[bytes]] = {}
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            sock.settimeout(max(0.1, deadline - time.time()))
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                break
            if data in PROBES:
                continue                     # our own broadcast echoed back
            seen.setdefault(addr[0], []).append(data[:200])
    finally:
        sock.close()
    return seen


def scan_ports(ip: str) -> list[int]:
    found = []
    for port in TCP_PORTS:
        sock = socket.socket()
        sock.settimeout(1.0)
        try:
            if sock.connect_ex((ip, port)) == 0:
                found.append(port)
        finally:
            sock.close()
    return found


def main() -> int:
    explicit = sys.argv[1] if len(sys.argv) > 1 else None
    targets = [explicit] if explicit else netutil.broadcast_addresses(config.LAN_CIDR_BITS)

    print(f"phantom-bridge probe -- {netutil.describe()}")
    print(f"looking for PN {config.DONGLE_PN}")
    print(f"targets: {', '.join(targets)}\n")

    hits: dict[str, list[bytes]] = {}
    for port in UDP_PORTS:
        print(f"UDP {port} ...")
        found = udp_sweep(targets, port)
        for ip, messages in found.items():
            hits.setdefault(ip, []).extend(messages)
        if found:
            for ip, messages in found.items():
                for msg in messages:
                    print(f"  <- {ip}: {msg!r}")
        else:
            print("  no reply")
    print()

    if not hits:
        print("No UDP reply on either config port.\n")
        print("Next steps, cheapest first:")
        print(f"  1. Router DHCP leases -> find the client matching "
              f"{config.DONGLE_PN}, then: python probe.py <that-ip>")
        print("  2. Confirm this machine is on the same WiFi as the inverter")
        print("     (this machine: " + netutil.describe() + ")")
        print("  3. Rule out AP/client isolation on the router -- it blocks "
              "exactly this kind of peer-to-peer traffic")
        print("  4. Windows Firewall can eat the *replies* even though the "
              "sends go out. Allow inbound UDP for python.exe, or test with "
              "the firewall briefly off.")
        return 1

    print("=" * 62)
    for ip in sorted(hits):
        open_ports = scan_ports(ip)
        print(f"\n[{ip}]  open TCP: {open_ports or 'none'}")
        for msg in hits[ip]:
            print(f"   <- {msg!r}")
        if config.DIRECT_TCP_PORT in open_ports:
            print(f"\n   BEST PATH: TCP {config.DIRECT_TCP_PORT} is open, so we can "
                  f"connect out to it.")
            print(f"   Nothing gets reconfigured and WatchPower keeps working:")
            print(f"       python ctl.py --direct {ip} read QPI QMOD QPIGS")
        else:
            print(f"\n   TCP {config.DIRECT_TCP_PORT} is closed, so the redirect path "
                  f"it is:")
            print(f"       python collector.py --dongle {ip}")
            print(f"   That silences WatchPower until:  python collector.py --restore")

    print("\n" + "=" * 62)
    print("Set DONGLE_IP in config.py to the address above to skip discovery.")
    print("\nBefore the dongle can reach us, Windows Firewall needs to let it in:")
    print("  " + netutil.firewall_hint(config.LOCAL_PORT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
