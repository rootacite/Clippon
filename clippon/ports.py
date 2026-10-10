"""GameStream port set and the host nftables table that exposes one instance.

Offsets match Sunshine 2026.1008.155609 with the default base port 47989:
HTTP +0, HTTPS -5, web UI +1, video UDP +9, control UDP +10, audio UDP +11,
RTSP +21.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence

BASE_PORT = 47989
SUBNET = "10.200.0.0/24"
BRIDGE = "cbr0"
BRIDGE_IP = "10.200.0.1"

# Moonlight dials these on the host. The gateway accepts the first three.
# Stream ports are DNATed into the selected instance.
GAMESTREAM_HTTP = 47989
GAMESTREAM_HTTPS = 47984
WEB_UI = 47990
TCP_PORTS = (GAMESTREAM_HTTPS, GAMESTREAM_HTTP, WEB_UI, 48010)
UDP_PORTS = (47998, 47999, 48000)
STREAM_TCP_PORTS = (48010,)
STREAM_UDP_PORTS = UDP_PORTS


def netns_name(name: str) -> str:
    return f"clippon-{name}"


def veth_names(name: str) -> tuple[str, str]:
    """Host-side and namespace-side interface names. Both stay within 15 bytes."""
    return f"vb-{name}", f"vn-{name}"


def allocate_host(used: set[int]) -> int:
    """Next free last octet in 10.200.0.0/24. .1 is the bridge."""
    for octet in range(2, 255):
        if octet not in used:
            return octet
    raise ValueError(f"no free address left in {SUBNET}")


def in_subnet(ip: str, subnet: str = SUBNET) -> bool:
    return ipaddress.ip_address(ip) in ipaddress.ip_network(subnet)


def nft_table(exposed_ip: str | None, lan: Sequence[str] = ()) -> str:
    """DNAT only the stream ports. 47984, 47989 and 47990 belong to the gateway.

    The forward chain's policy accept ends this chain only; a later chain can
    still drop the packet. Docker's DOCKER-USER chain is updated separately.

    *lan* holds this host's own addresses. Traffic a client on this host sends
    to one of them is generated locally, so it passes the output hook and never
    reaches prerouting; the output chain repeats the same DNAT for it.
    """
    tcp = _port_set(STREAM_TCP_PORTS)
    udp = _port_set(STREAM_UDP_PORTS)
    dnat = ""
    local = ""
    if exposed_ip is not None:
        if not in_subnet(exposed_ip):
            raise ValueError(f"exposed address {exposed_ip} is outside {SUBNET}")
        dnat = (
            f"    tcp dport {tcp} dnat to {exposed_ip}\n"
            f"    udp dport {udp} dnat to {exposed_ip}\n"
        )
        if lan:
            addresses = ", ".join(sorted(set(lan)))
            local = (
                f"    ip daddr {{ {addresses} }} tcp dport {tcp} dnat to {exposed_ip}\n"
                f"    ip daddr {{ {addresses} }} udp dport {udp} dnat to {exposed_ip}\n"
            )
    return (
        "table ip clippon {\n"
        "  chain prerouting {\n"
        "    type nat hook prerouting priority dstnat; policy accept;\n"
        f"{dnat}"
        "  }\n"
        "  chain output {\n"
        "    type nat hook output priority dstnat; policy accept;\n"
        f"{local}"
        "  }\n"
        "  chain postrouting {\n"
        "    type nat hook postrouting priority srcnat; policy accept;\n"
        f"    ip saddr {SUBNET} masquerade\n"
        "  }\n"
        "  chain forward {\n"
        "    type filter hook forward priority filter; policy accept;\n"
        f"    ip daddr {SUBNET} tcp dport {tcp} accept\n"
        f"    ip daddr {SUBNET} udp dport {udp} accept\n"
        f"    ip saddr {SUBNET} accept\n"
        "  }\n"
        "}\n"
    )


def docker_user_checks(subnet: str = SUBNET) -> tuple[tuple[list[str], list[str]], tuple[list[str], list[str]]]:
    """Two (check, insert) pairs for Docker's DOCKER-USER chain."""
    destinations = (
        ["iptables", "-C", "DOCKER-USER", "-d", subnet, "-j", "ACCEPT"],
        ["iptables", "-I", "DOCKER-USER", "1", "-d", subnet, "-j", "ACCEPT"],
    )
    sources = (
        ["iptables", "-C", "DOCKER-USER", "-s", subnet, "-j", "ACCEPT"],
        ["iptables", "-I", "DOCKER-USER", "1", "-s", subnet, "-j", "ACCEPT"],
    )
    return destinations, sources


def lan_from_ip_json(payload: list[object], subnet: str = SUBNET) -> list[str]:
    """Global IPv4 addresses a Moonlight client can dial, from `ip -json addr`."""
    found: list[str] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        ifname = str(raw.get("ifname", ""))
        if _skipped_interface(ifname):
            continue
        infos = raw.get("addr_info")
        if not isinstance(infos, list):
            continue
        for info in infos:
            if not isinstance(info, dict):
                continue
            if info.get("family") not in (None, "inet"):
                continue
            if info.get("scope") != "global":
                continue
            local = info.get("local")
            if not isinstance(local, str) or in_subnet(local, subnet):
                continue
            found.append(local)
    return found


def _port_set(ports: tuple[int, ...]) -> str:
    return "{ " + ", ".join(str(port) for port in ports) + " }"


def _skipped_interface(name: str) -> bool:
    if name in {BRIDGE, "lo", "docker0"}:
        return True
    return name.startswith(("vb-", "vn-", "docker", "br-", "veth", "virbr"))
