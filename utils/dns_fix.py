"""
dns_fix.py
==========
Applies a socket-level DNS workaround when the local resolver can't resolve
a hostname. Falls back to querying 8.8.8.8 (Google DNS) to get the IP, then
patches socket.getaddrinfo for that host only.

Safe to call multiple times — only patches once per host.
Used to allow local dev machines with broken DNS to reach Supabase.
"""

import socket
import subprocess
import logging

logger = logging.getLogger(__name__)

_patched_hosts: set[str] = set()
_orig_getaddrinfo = socket.getaddrinfo


def ensure_host_reachable(host: str) -> None:
    """
    Check if `host` resolves via the system DNS.
    If not, fall back to Google DNS (8.8.8.8) and patch socket.getaddrinfo.
    No-op if already patched or if system DNS works.
    """
    if not host or host in _patched_hosts:
        return

    # Try system DNS first
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return  # system DNS works fine
    except OSError:
        pass

    # System DNS failed — query Google DNS for the IP
    try:
        result = subprocess.run(
            ["nslookup", host, "8.8.8.8"],
            capture_output=True, text=True, timeout=5,
        )
        ip = None
        for line in result.stdout.splitlines():
            line = line.strip()
            # Match both "Address:" and "Addresses:" lines, skip the DNS server itself
            if line.lower().startswith("address") and "8.8.8.8" not in line:
                candidate = line.split(":", 1)[-1].strip()
                # Prefer IPv4; skip IPv6 (contains colons)
                if candidate and ":" not in candidate:
                    ip = candidate
                    break
        if not ip:
            logger.warning("dns_fix: could not resolve %s via 8.8.8.8", host)
            return
    except Exception as exc:
        logger.warning("dns_fix: nslookup failed for %s: %s", host, exc)
        return

    # Patch socket.getaddrinfo for this host only
    resolved_ip = ip
    target_host = host

    original = socket.getaddrinfo

    def _patched(h, port, *args, **kwargs):
        if h == target_host:
            h = resolved_ip
        return original(h, port, *args, **kwargs)

    socket.getaddrinfo = _patched
    _patched_hosts.add(host)
    logger.info("dns_fix: patched DNS for %s -> %s", host, resolved_ip)
