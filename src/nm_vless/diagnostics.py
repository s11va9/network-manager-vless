# SPDX-License-Identifier: GPL-2.0-or-later
"""Detect configurations that make traffic bypass the VLESS tunnel.

Two situations are common after switching from another client:

* another tunnel (for example a previous VPN client's TUN device) has routes
  that win over the VLESS routes, so the kernel sends traffic there instead;
* the desktop's system proxy still points to another client's local proxy
  (127.0.0.1:…), so browsers and other applications talk to that proxy directly.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

__all__ = [
    "TUN_PREFIX",
    "DefaultRoute",
    "competing_tunnels",
    "default_routes",
    "env_local_proxies",
    "gnome_proxy_settings",
    "is_tun_device",
    "local_proxy",
    "own_devices",
]

#: Name prefix of the TUN devices created by nm-vless-service (see tun.DEFAULT_NAME_TEMPLATE).
TUN_PREFIX = "vless"


@dataclass(frozen=True, slots=True)
class DefaultRoute:
    """A default route, or one half of the address space (``0.0.0.0/1``, ``::/1``)."""

    dev: str
    metric: int
    family: int  # 4 or 6
    prefix: int = 0  # 0 or 1

    @property
    def destination(self) -> str:
        if not self.prefix:
            return "default"
        return "::/1" if self.family == 6 else "0.0.0.0/1"  # noqa: PLR2004

    def beats(self, other: DefaultRoute) -> bool:
        """True if the kernel prefers this route (ties count as winning)."""
        return (-self.prefix, self.metric) <= (-other.prefix, other.metric)


def is_tun_device(dev: str) -> bool:
    """True for TUN/TAP devices (the kind other VPN clients create)."""
    return Path("/sys/class/net", dev, "tun_flags").exists()


#: Prefixes queried for each family: the default route and the lower half of the
#: address space, which VPNs add together with the upper half to override it.
_QUERIES = ((4, "default", 0), (4, "0.0.0.0/1", 1), (6, "default", 0), (6, "::/1", 1))


def default_routes() -> list[DefaultRoute]:
    """Default and half-default routes of the main table, from ``ip -json route``."""
    routes: list[DefaultRoute] = []
    for family, prefix_text, prefix in _QUERIES:
        try:
            output = subprocess.run(
                ["/usr/sbin/ip", "-json", f"-{family}", "route", "show", prefix_text],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout
            entries = json.loads(output or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        routes.extend(
            DefaultRoute(
                dev=str(e["dev"]), metric=int(e.get("metric", 0)), family=family, prefix=prefix
            )
            for e in entries
            if e.get("dev")
        )
    return routes


def own_devices(routes: Collection[DefaultRoute]) -> set[str]:
    """Devices of active VLESS tunnels among *routes*."""
    return {r.dev for r in routes if r.dev.startswith(TUN_PREFIX)}


def competing_tunnels(
    routes: Collection[DefaultRoute],
    own_devices: Collection[str],
    is_tun: Callable[[str], bool] = is_tun_device,
) -> list[DefaultRoute]:
    """Routes of foreign tunnels that win over (or would win over) the VLESS routes.

    VLESS tunnels route both halves of the address space, so a plain default route
    of another tunnel never wins over them. With no VLESS tunnel up, foreign
    half-default routes are reported, because they would keep winning; with one up
    but not for a family (IPv6 turned off), every foreign route of that family is.
    """
    result: list[DefaultRoute] = []
    vless_up = any(r.dev in own_devices for r in routes)
    for family in (4, 6):
        own = [r for r in routes if r.family == family and r.dev in own_devices]
        result.extend(
            r
            for r in routes
            if r.family == family
            and r.dev not in own_devices
            and is_tun(r.dev)
            and (all(r.beats(o) for o in own) if own else vless_up or r.prefix > 0)
        )
    return result


def _is_local(host: str) -> bool:
    host = host.strip().strip("[]")
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_proxy(settings: Mapping[str, object]) -> str | None:
    """Return ``host:port`` if the system proxy points to a local proxy server.

    *settings* holds the GNOME keys ``mode`` and ``http.host``/``http.port``,
    ``https.host``/…, ``socks.host``/… (see org.gnome.system.proxy).
    """
    if settings.get("mode") != "manual":
        return None
    for kind in ("https", "http", "socks"):
        host = str(settings.get(f"{kind}.host") or "")
        if host and _is_local(host):
            return f"{host}:{settings.get(f'{kind}.port')}"
    return None


_PROXY_VARS = ("https_proxy", "http_proxy", "all_proxy")


def env_local_proxies(environ: Mapping[str, str]) -> list[str]:
    """Names of proxy environment variables in *environ* that point to a local proxy.

    Programs started from a session with these variables (terminals, command-line
    tools, Electron apps that pass them on) keep using that proxy.
    """
    names: list[str] = []
    for name, value in sorted(environ.items()):
        if name.lower() not in _PROXY_VARS or not value.strip():
            continue
        target = value.strip()
        host = urlsplit(target if "://" in target else f"//{target}").hostname or ""
        if _is_local(host):
            names.append(name)
    return names


def gnome_proxy_settings() -> dict[str, object] | None:
    """Read org.gnome.system.proxy, or None outside GNOME."""
    from nm_vless.proxyguard import GnomeProxy  # noqa: PLC0415 - GObject, only on desktops

    return GnomeProxy().values() if GnomeProxy.available() else None
