"""GameStream port groups and the host nftables table that maps them.

Every instance's Sunshine listens on a base port of its own, and the rest of
its ports are fixed offsets from that base (Sunshine 2026.1008.155609:
src/stream.h, src/rtsp.h, src/nvhttp.h, src/confighttp.h, src/config.cpp):

    HTTP +0, HTTPS -5, web UI +1, video UDP +9, control UDP +10,
    audio UDP +11, RTSP +21

The host maps each instance's stream ports to that instance's veth address,
so two sessions never fight over a port. The gateway keeps the first group's
47984/47989/47990 for itself.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

BASE_PORT = 47989
# Instance groups are this far apart: a group spans base-5 .. base+21.
PORT_STRIDE = 1000
# What Sunshine accepts as a base port (config.cpp, int_between_f).
MIN_BASE_PORT = 1019
MAX_BASE_PORT = 65514

SUBNET = "10.200.0.0/24"
BRIDGE = "cbr0"
BRIDGE_IP = "10.200.0.1"

# The gateway's own listeners, on the first group's control ports.
GAMESTREAM_HTTP = BASE_PORT
GAMESTREAM_HTTPS = BASE_PORT - 5
WEB_UI = BASE_PORT + 1


@dataclass(frozen=True)
class PortGroup:
    """One instance's ports. Everything is derived from its base port."""

    base: int

    @property
    def http(self) -> int:
        return self.base

    @property
    def https(self) -> int:
        return self.base - 5

    @property
    def web(self) -> int:
        return self.base + 1

    @property
    def rtsp(self) -> int:
        return self.base + 21

    @property
    def video(self) -> int:
        return self.base + 9

    @property
    def control(self) -> int:
        return self.base + 10

    @property
    def audio(self) -> int:
        return self.base + 11

    @property
    def tcp(self) -> tuple[int, ...]:
        return (self.rtsp,)

    @property
    def udp(self) -> tuple[int, ...]:
        return (self.video, self.control, self.audio)


def port_group(base: int = BASE_PORT) -> PortGroup:
    return PortGroup(base)


def base_port_ok(base: object) -> bool:
    return isinstance(base, int) and MIN_BASE_PORT <= base <= MAX_BASE_PORT


def allocate_base_port(used: Iterable[int]) -> int:
    """The next free group. Groups are PORT_STRIDE apart and never overlap."""
    taken = set(used)
    base = BASE_PORT
    while base <= MAX_BASE_PORT:
        if base not in taken:
            return base
        base += PORT_STRIDE
    raise ValueError(f"no free port group in {MIN_BASE_PORT}..{MAX_BASE_PORT}")


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


def nft_table(instances: Sequence[tuple[str, int]] = (), lan: Sequence[str] = ()) -> str:
    """DNAT every instance's stream ports to that instance's veth address.

    *instances* holds (address, base port) pairs. 47984, 47989 and 47990 belong
    to the gateway and are never in here.

    The forward chain's policy accept ends this chain only; a later chain can
    still drop the packet. Docker's DOCKER-USER chain is updated separately.

    *lan* holds this host's own addresses. Traffic a client on this host sends
    to one of them is generated locally, so it passes the output hook and never
    reaches prerouting; the output chain repeats the same DNAT for it.
    """
    addresses = ", ".join(sorted(set(lan)))
    dnat: list[str] = []
    local: list[str] = []
    tcp: list[int] = []
    udp: list[int] = []
    for ip, base in instances:
        if not in_subnet(ip):
            raise ValueError(f"instance address {ip} is outside {SUBNET}")
        group = port_group(base)
        dnat.append(f"    tcp dport {_port_set(group.tcp)} dnat to {ip}\n")
        dnat.append(f"    udp dport {_port_set(group.udp)} dnat to {ip}\n")
        if addresses:
            local.append(
                f"    ip daddr {{ {addresses} }} tcp dport {_port_set(group.tcp)} dnat to {ip}\n"
            )
            local.append(
                f"    ip daddr {{ {addresses} }} udp dport {_port_set(group.udp)} dnat to {ip}\n"
            )
        tcp.extend(group.tcp)
        udp.extend(group.udp)
    forward = ""
    if tcp:
        forward = (
            f"    ip daddr {SUBNET} tcp dport {_port_set(tuple(tcp))} accept\n"
            f"    ip daddr {SUBNET} udp dport {_port_set(tuple(udp))} accept\n"
        )
    return (
        "table ip clippon {\n"
        "  chain prerouting {\n"
        "    type nat hook prerouting priority dstnat; policy accept;\n"
        f"{''.join(dnat)}"
        "  }\n"
        "  chain output {\n"
        "    type nat hook output priority dstnat; policy accept;\n"
        f"{''.join(local)}"
        "  }\n"
        "  chain postrouting {\n"
        "    type nat hook postrouting priority srcnat; policy accept;\n"
        f"    ip saddr {SUBNET} masquerade\n"
        "  }\n"
        "  chain forward {\n"
        "    type filter hook forward priority filter; policy accept;\n"
        f"{forward}"
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
