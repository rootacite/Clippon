"""GameStream documents the gateway owns, and the rewrites it applies.

47989 and 47984 are answered here. sessionUrl0 is rewritten onto the address
the client actually dialed. Stream ports stay on kernel DNAT.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zlib

_URL = re.compile(r"(rtspenc|rtsp)://(\[[^\]]+\]|[^:/]+):(\d+)\s*$")
_APPID = re.compile(r"([?&]appid=)[^&]*")

LOCAL_PATHS = frozenset({"/serverinfo"})
FORWARD_PATHS = frozenset({"/pair", "/applist", "/appasset", "/launch", "/resume", "/cancel"})
APP_VERSION = "7.1.431.-1"
GFE_VERSION = "3.23.0.74"


def request_path(target: str) -> str:
    return target.split("?", 1)[0]


def classify(target: str) -> str:
    """local: the gateway answers. forward: pass the request to a backend."""
    path = request_path(target)
    if path in LOCAL_PATHS:
        return "local"
    if path in FORWARD_PATHS:
        return "forward"
    return "reject"


def replace_query_appid(target: str, internal: str) -> str:
    if not _APPID.search(target):
        return target
    return _APPID.sub(rf"\g<1>{internal}", target, count=1)


def serverinfo_xml(
    *,
    local_ip: str,
    unique_id: str,
    hostname: str = "clippon",
    paired: bool = False,
    current_game: int = 0,
    codec_flags: str = "0",
    max_luma: str = "0",
    mac: str = "00:00:00:00:00:00",
) -> str:
    state = "SUNSHINE_SERVER_BUSY" if current_game else "SUNSHINE_SERVER_FREE"
    pair = "1" if paired else "0"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<root status_code="200">'
        f"<hostname>{hostname}</hostname>"
        f"<appversion>{APP_VERSION}</appversion>"
        f"<GfeVersion>{GFE_VERSION}</GfeVersion>"
        f"<uniqueid>{unique_id}</uniqueid>"
        "<HttpsPort>47984</HttpsPort>"
        "<ExternalPort>47989</ExternalPort>"
        f"<MaxLumaPixelsHEVC>{max_luma}</MaxLumaPixelsHEVC>"
        f"<mac>{mac}</mac>"
        f"<LocalIP>{local_ip}</LocalIP>"
        f"<ServerCodecModeSupport>{codec_flags}</ServerCodecModeSupport>"
        f"<PairStatus>{pair}</PairStatus>"
        f"<currentgame>{current_game}</currentgame>"
        f"<state>{state}</state>"
        "</root>"
    )


def xml_text(document: str, tag: str) -> str | None:
    try:
        root = ET.fromstring(document)
    except ET.ParseError:
        return None
    found = root.findtext(tag)
    return found if found is not None else None


def external_app_id(instance: str, internal: str) -> int:
    """Stable positive id Moonlight can store. It is not reversible; keep the table."""
    value = zlib.crc32(f"{instance}\0{internal}".encode()) & 0x7FFFFFFF
    return value or 1


def merge_applists(
    sources: list[tuple[str, str]],
) -> tuple[str, dict[int, tuple[str, str]]]:
    """Merge backend applist documents. The title Moonlight shows is the instance name.

    Returns the combined XML and a map of external id -> (instance, internal id).
    """
    root = ET.Element("root")
    root.set("status_code", "200")
    table: dict[int, tuple[str, str]] = {}
    for instance, xml in sources:
        parsed = ET.fromstring(xml)
        for app in parsed.iter("App"):
            internal = app.findtext("ID") or ""
            external = external_app_id(instance, internal)
            while external in table and table[external] != (instance, internal):
                external = (external + 1) & 0x7FFFFFFF or 1
            table[external] = (instance, internal)
            node = ET.SubElement(root, "App")
            ET.SubElement(node, "AppTitle").text = instance
            ET.SubElement(node, "ID").text = str(external)
            hdr = app.findtext("IsHdrSupported")
            if hdr is not None:
                ET.SubElement(node, "IsHdrSupported").text = hdr
    return ET.tostring(root, encoding="unicode"), table


def rewrite_session_host(xml: str, host: str) -> str:
    """Point sessionUrl0 at `host`, keeping the scheme and port Sunshine chose."""
    root = ET.fromstring(xml)
    found = False
    for element in root.iter("sessionUrl0"):
        if not element.text:
            continue
        element.text = replace_url_host(element.text.strip(), host)
        found = True
    if not found:
        raise ValueError("sessionUrl0 is missing")
    return ET.tostring(root, encoding="unicode")


def replace_url_host(url: str, host: str) -> str:
    match = _URL.fullmatch(url.strip())
    if match is None:
        raise ValueError(f"unrecognized session url {url!r}")
    shown = host
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        shown = f"[{host}]"
    return f"{match.group(1)}://{shown}:{match.group(3)}"
