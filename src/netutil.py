"""LAN helpers. Windows-first, stdlib only.

The spike hardcoded LOCAL_IP and broadcast to 255.255.255.255. Both bite on a
Windows laptop: the address changes with DHCP, and a limited broadcast often
leaves by the wrong interface when Wi-Fi and Ethernet are both up. These
helpers pick the right values and say what they assumed.
"""
from __future__ import annotations

import socket


def primary_lan_ip(probe_host: str = "8.8.8.8") -> str | None:
    """The source address this machine would use toward the default route.

    No packet is sent -- connect() on a UDP socket only sets the route.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((probe_host, 9))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def local_ipv4_addresses() -> list[str]:
    """Every non-loopback IPv4 this host answers to, primary route first."""
    found: list[str] = []
    primary = primary_lan_ip()
    if primary:
        found.append(primary)
    try:
        _host, _alias, addrs = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        addrs = []
    for addr in addrs:
        if addr not in found and not addr.startswith("127."):
            found.append(addr)
    return found


def broadcast_addresses(cidr_bits: int | None = None) -> list[str]:
    """Per-interface broadcast addresses, plus the limited broadcast as a fallback.

    Netmasks are not discoverable from the stdlib on Windows, so a /24 is
    assumed unless `cidr_bits` says otherwise (config.LAN_CIDR_BITS). A /24 is
    right on essentially every home router; if the site is not one, set it.
    """
    bits = cidr_bits or 24
    out: list[str] = []
    for addr in local_ipv4_addresses():
        try:
            packed = int.from_bytes(socket.inet_aton(addr), "big")
        except OSError:
            continue
        host_mask = (1 << (32 - bits)) - 1
        bcast = socket.inet_ntoa(((packed | host_mask) & 0xFFFFFFFF).to_bytes(4, "big"))
        if bcast not in out:
            out.append(bcast)
    out.append("255.255.255.255")
    return out


def firewall_hint(port: int) -> str:
    """The netsh line that lets the dongle reach our listener.

    Windows Firewall silently drops the dongle's inbound TCP connection on a
    Public profile network, which looks exactly like "the redirect did not
    work". Run this once, elevated.
    """
    return (
        f'netsh advfirewall firewall add rule name="phantom-bridge {port}" '
        f'dir=in action=allow protocol=TCP localport={port}'
    )


def describe() -> str:
    """One-line summary of what we think the network looks like."""
    primary = primary_lan_ip() or "unknown"
    others = [a for a in local_ipv4_addresses() if a != primary]
    extra = f" (also {', '.join(others)})" if others else ""
    return f"local IP {primary}{extra}"
