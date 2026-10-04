# SPDX-License-Identifier: GPL-2.0-or-later
"""Xray-core integration: locating the binary and generating its configuration.

The generated configuration contains a single TUN inbound attached to a device
created by the service (``XRAY_TUN_FD``) and a single VLESS outbound. Xray does
not configure addresses or routes; NetworkManager does.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from nm_vless.buildinfo import build_value
from nm_vless.link import VlessServer, is_ip_address

__all__ = [
    "DEFAULT_CANDIDATES",
    "ENV_XRAY",
    "MIN_VERSION",
    "READY_RE",
    "XrayError",
    "build_config",
    "check_binary",
    "ensure_supported",
    "find_xray",
    "parse_version",
    "version",
]

#: Environment variable with an explicit path to the xray binary.
ENV_XRAY: Final = "NM_VLESS_XRAY"
SYSTEM_CANDIDATES: Final = ("/usr/local/bin/xray", "/usr/bin/xray")


#: The xray shipped with the package comes first, then a system-wide installation.
DEFAULT_CANDIDATES: Final = tuple(c for c in (build_value("BUNDLED_XRAY"), *SYSTEM_CANDIDATES) if c)

#: First release that accepts a TUN device via ``XRAY_TUN_FD`` on Linux (XTLS/Xray-core#6338).
MIN_VERSION: Final = (26, 6, 22)
_VERSION_RE: Final = re.compile(r"^Xray (\d+)\.(\d+)\.(\d+)\b")

#: Xray logs this line (level "warning") once all inbounds and outbounds are running.
READY_RE: Final = re.compile(r"\bXray \S+ started\b")

TUN_INBOUND_TAG: Final = "tun-in"
PROXY_OUTBOUND_TAG: Final = "proxy"
#: Send the server the site's domain (from TLS SNI, the HTTP Host or QUIC) instead of
#: the IP address the application connected to, as other Xray clients do. Servers
#: often route by domain (for example some sites through another exit); with only
#: an IP address such connections do not match and are dropped.
SNIFFING: Final = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": False}


class XrayError(RuntimeError):
    """The xray binary is missing, unsafe or failed to run."""


def check_binary(path: str | os.PathLike[str]) -> Path:
    """Ensure *path* is a regular executable file that only root can modify.

    The service hands the tunnel to this binary, so a binary writable by other
    users would let them read and modify all tunnelled traffic.
    """
    resolved = Path(path).resolve(strict=False)
    try:
        info = resolved.stat()
    except OSError as exc:
        raise XrayError(f"{path}: {exc.strerror}") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise XrayError(f"{resolved} is not an executable file")
    for item in (resolved, *resolved.parents):
        item_info = item.stat()
        if item_info.st_uid != 0 or item_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            if item_info.st_mode & stat.S_ISVTX and item != resolved:
                continue  # sticky directory such as /tmp: entries are protected
            raise XrayError(f"{item} must be owned by root and not writable by others")
    return resolved


def find_xray(env: Mapping[str, str] | None = None) -> Path:
    """Return the xray binary to use.

    ``$NM_VLESS_XRAY`` wins if set; otherwise the first existing default
    candidate is used. The result is checked with :func:`check_binary`.
    """
    env = os.environ if env is None else env
    explicit = env.get(ENV_XRAY, "").strip()
    if explicit:
        return check_binary(explicit)
    for candidate in DEFAULT_CANDIDATES:
        if Path(candidate).exists():
            return check_binary(candidate)
    raise XrayError(
        "xray binary not found; reinstall the package or install Xray-core to /usr/local/bin/xray "
        f"or set {ENV_XRAY} (see README)"
    )


def version(
    path: str | os.PathLike[str], *, prefix: Sequence[str] = (), timeout: float = 5.0
) -> str:
    """Return the first line of ``xray version``, run behind the optional command *prefix*."""
    try:
        result = subprocess.run(
            [*prefix, os.fspath(path), "version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise XrayError(f"cannot run {path}: {exc}") from exc
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip().splitlines()
        reason = output[-1] if output else f"exit status {result.returncode}"
        raise XrayError(f"cannot run {path}: {reason}")
    return result.stdout.splitlines()[0] if result.stdout else "unknown"


def parse_version(line: str) -> tuple[int, int, int] | None:
    """Extract the version from the first line of ``xray version``."""
    match = _VERSION_RE.match(line.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def ensure_supported(path: str | os.PathLike[str], *, prefix: Sequence[str] = ()) -> str:
    """Return the version line of *path*, or raise if it is older than :data:`MIN_VERSION`."""
    line = version(path, prefix=prefix)
    parsed = parse_version(line)
    if parsed is None:
        raise XrayError(f"cannot determine the version of {path}: {line[:80]!r}")
    if parsed < MIN_VERSION:
        found = ".".join(map(str, parsed))
        required = ".".join(map(str, MIN_VERSION))
        raise XrayError(
            f"{path} is Xray {found}; version {required} or newer is required "
            "(support for XRAY_TUN_FD)"
        )
    return line


def _stream_settings(server: VlessServer) -> dict[str, Any]:
    # The outbound connects to a pre-resolved IP address. Keep TLS and HTTP
    # working by falling back to the original domain for SNI and Host.
    domain = "" if is_ip_address(server.address) else server.address
    stream: dict[str, Any] = {"network": server.network, "security": server.security}

    if server.security == "tls":
        tls: dict[str, Any] = {}
        if server.sni or domain:
            tls["serverName"] = server.sni or domain
        if server.fingerprint:
            tls["fingerprint"] = server.fingerprint
        if server.alpn:
            tls["alpn"] = list(server.alpn)
        stream["tlsSettings"] = tls
    elif server.security == "reality":
        stream["realitySettings"] = {
            "serverName": server.sni,
            "fingerprint": server.fingerprint or "chrome",
            "publicKey": server.public_key,
            "shortId": server.short_id,
            "spiderX": server.spider_x,
        }

    host = server.host or domain
    if server.network in ("ws", "httpupgrade", "xhttp"):
        http: dict[str, Any] = {"path": server.path or "/"}
        if host:
            http["host"] = host
        if server.network == "xhttp":
            http["mode"] = server.mode or "auto"
            if server.extra:
                http["extra"] = _json_object(server.extra)
        stream[f"{server.network}Settings"] = http
    elif server.network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": server.service_name,
            "multiMode": server.mode == "multi",
        }
    return stream


def _json_object(text: str) -> dict[str, Any]:
    value = json.loads(text)
    if not isinstance(value, dict):  # guaranteed by VlessServer validation
        raise XrayError("xhttp extra must be a JSON object")
    return value


def build_config(
    server: VlessServer,
    *,
    connect_address: str,
    tun_name: str,
    mtu: int,
    log_level: str = "warning",
) -> dict[str, Any]:
    """Build the Xray-core JSON configuration for one connection.

    Args:
        server: validated server description.
        connect_address: IP address of the server, resolved by the service
            before the tunnel exists. NetworkManager routes exactly this address
            outside the tunnel, which prevents routing loops.
        tun_name: name of the TUN device passed via ``XRAY_TUN_FD``.
        mtu: MTU of the TUN device.
        log_level: Xray log level; "warning" or more verbose is required for
            the readiness message matched by :data:`READY_RE`.
    """
    if not is_ip_address(connect_address):
        raise XrayError("connect_address must be an IP address")
    user: dict[str, Any] = {"id": server.uuid, "encryption": server.encryption}
    if server.flow:
        user["flow"] = server.flow
    return {
        "log": {"loglevel": log_level, "access": "none", "dnsLog": False},
        "inbounds": [
            {
                "tag": TUN_INBOUND_TAG,
                "port": 0,
                "protocol": "tun",
                "settings": {"name": tun_name, "mtu": mtu},
                "sniffing": SNIFFING,
            }
        ],
        "outbounds": [
            {
                "tag": PROXY_OUTBOUND_TAG,
                "protocol": "vless",
                "settings": {
                    "vnext": [{"address": connect_address, "port": server.port, "users": [user]}]
                },
                "streamSettings": _stream_settings(server),
            }
        ],
    }
