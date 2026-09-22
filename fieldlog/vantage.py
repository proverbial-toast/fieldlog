"""Where a run was made from: the interface, local address and gateway the
target was reached through, and the wireless network when there is one.

A ping that lost 20% means one thing from the switch port and another from the
guest Wi-Fi or over the VPN, and a week later the command line alone cannot say
which it was. So each run record carries a `vantage` block, read from the OS's
own routing answer at the moment the run starts.

Best effort, always: every probe is bounded, and any failure leaves the field
out (or the whole block, when not even the interface is known) rather than
guessing. Nothing here resolves a hostname — a DNS lookup can hang, and the
route it would pick is the default route anyway.
"""

from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
import sys
from typing import Callable, Dict, List, Optional

from fieldlog.state import get_interface_ip

# One probe's wall-clock limit. A routing-table answer is a few milliseconds;
# this only bounds a wedged binary.
PROBE_TIMEOUT = 1.0

# The keys a vantage block can carry, in the order readers show them.
VANTAGE_KEYS = ("iface", "local", "gateway", "ssid", "route")

Runner = Callable[[List[str]], str]


def _run(argv: List[str]) -> str:
    """stdout of `argv`, '' when it is missing, fails or overruns."""
    if not shutil.which(argv[0]):
        return ""
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def probe_address(target: str) -> tuple[Optional[str], str]:
    """`(address to route to, zone)` for a target, or `(None, '')` when the
    target is no literal address and the default route is the answer.

    A subnet routes like its network address, `user@host` like its host, and
    an IPv6 zone (`fe80::1%eth0`) names the interface outright.
    """
    text = (target or "").strip()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    zone = ""
    if "%" in text:
        text, zone = text.split("%", 1)
    try:
        if "/" in text:
            return str(ipaddress.ip_network(text, strict=False).network_address), zone
        return str(ipaddress.ip_address(text)), zone
    except ValueError:
        return None, ""


def _word_after(text: str, word: str) -> str:
    match = re.search(rf"(?:^|\s){re.escape(word)}\s+(\S+)", text)
    return match[1] if match else ""


def _route_linux(address: Optional[str], run: Runner) -> Dict[str, str]:
    """`ip route get` for an address, else the default route."""
    if address:
        out = run(["ip", "-o", "route", "get", address])
    else:
        out = (run(["ip", "-o", "route", "show", "default"]).splitlines() or [""])[0]
    return {"iface": _word_after(out, "dev"), "local": _word_after(out, "src"),
            "gateway": _word_after(out, "via")}


def _route_darwin(address: Optional[str], run: Runner) -> Dict[str, str]:
    """`route -n get` for an address, else the default route."""
    family = ["-inet6"] if address and ":" in address else []
    out = run(["route", "-n", "get", *family, address or "default"])
    found = dict(re.findall(r"^\s*(\w+):\s*(\S+)", out, re.MULTILINE))
    gateway = found.get("gateway", "")
    # An on-link destination names itself or a link address, not a router.
    if gateway.startswith("link#") or gateway == address:
        gateway = ""
    return {"iface": found.get("interface", ""), "local": "", "gateway": gateway}


def _ssid(iface: str, platform: str, run: Runner) -> str:
    """The wireless network `iface` is joined to, '' for a wired one or none known."""
    if platform == "darwin":
        out = run(["ipconfig", "getsummary", iface])
        match = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", out, re.MULTILINE)
    else:
        out = run(["iw", "dev", iface, "link"])
        match = re.search(r"^\s*SSID:\s*(.+?)\s*$", out, re.MULTILINE)
        if not match:
            name = run(["iwgetid", iface, "-r"]).strip()
            return name
    ssid = match[1] if match else ""
    # macOS 14.4+ hides the name from `ipconfig` unless the operator has turned
    # its verbose mode on; the placeholder is not a network name.
    return "" if ssid.lower() == "<redacted>" else ssid


def vantage(target: str, platform: Optional[str] = None, run: Runner = _run) -> Dict[str, str]:
    """The vantage block for a run against `target`; {} when the route is unknown.

    `route` says which question was answered: `target` when the OS was asked
    about the target's own address, `default` when it was a hostname (or no
    target) and the default route stands in.
    """
    platform = platform or sys.platform
    address, zone = probe_address(target)
    try:
        if platform == "darwin":
            found = _route_darwin(address, run)
        else:
            found = _route_linux(address, run)
    except Exception:  # noqa: BLE001 — a vantage we cannot read is not a failed run
        return {}
    iface = found.get("iface") or zone
    if not iface:
        return {}
    local = found.get("local") or get_interface_ip(iface)
    try:
        ssid = _ssid(iface, platform, run)
    except Exception:  # noqa: BLE001
        ssid = ""
    block = {
        "iface": iface,
        "local": local,
        "gateway": found.get("gateway", ""),
        "ssid": ssid,
        "route": "target" if address else "default",
    }
    return {k: block[k] for k in VANTAGE_KEYS if block[k]}


def vantage_line(block: Optional[dict]) -> str:
    """`wlan0 · 192.168.4.23 · via 192.168.4.1 · ssid Office-Guest`, or ''."""
    if not isinstance(block, dict) or not block.get("iface"):
        return ""
    bits = [str(block["iface"])]
    if block.get("local"):
        bits.append(str(block["local"]))
    if block.get("gateway"):
        bits.append(f"via {block['gateway']}")
    if block.get("ssid"):
        bits.append(f"ssid {block['ssid']}")
    return " · ".join(bits)
