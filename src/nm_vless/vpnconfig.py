# SPDX-License-Identifier: GPL-2.0-or-later
"""Build the IP configuration the VPN service reports to NetworkManager.

NetworkManager expects IPv4 addresses as ``u`` in network byte order read as a
native integer, and IPv6 addresses as ``ay`` (see ``_vardict_to_addr`` in
NetworkManager's src/core/vpn/nm-vpn-connection.c).
"""

from __future__ import annotations

import socket
import sys
from ipaddress import IPv4Address, IPv4Interface, IPv6Address, IPv6Interface
from typing import Final

import gi

gi.require_version("NM", "1.0")
from gi.repository import NM, GLib

from nm_vless.settings import TunnelOptions

__all__ = [
    "SPLIT_DEFAULT4",
    "SPLIT_DEFAULT6",
    "TUN_ADDRESS4",
    "TUN_ADDRESS6",
    "generic_config",
    "ip4_config",
    "ip4_to_u32",
    "ip6_config",
]

#: Local tunnel addresses. 198.18.0.0/15 is reserved for benchmarking (RFC 2544)
#: and does not collide with home, Docker or CGNAT networks; the IPv6 prefix is
#: a unique local address (RFC 4193). Xray accepts packets for any destination.
TUN_ADDRESS4: Final = IPv4Interface("198.18.0.1/30")
TUN_ADDRESS6: Final = IPv6Interface("fd6e:6d76:6c65::1/64")

#: Two halves of the address space. They are more specific than any default route,
#: so the tunnel wins over other VPN clients' default routes whatever their metric
#: (another Xray client's TUN device commonly uses metric 1, below the VPN's 50).
#: The route to the server itself is a host route and stays more specific still.
SPLIT_DEFAULT4: Final = ("0.0.0.0", "128.0.0.0")  # noqa: S104 - routes, not sockets
SPLIT_DEFAULT6: Final = ("::", "8000::")


def ip4_to_u32(address: IPv4Address) -> int:
    """Encode *address* the way NetworkManager reads IPv4 values from D-Bus."""
    return int.from_bytes(address.packed, sys.byteorder)


def _ip6_bytes(address: IPv6Address) -> GLib.Variant:
    return GLib.Variant("ay", address.packed)


def generic_config(
    *, tundev: str, mtu: int, gateway: IPv4Address | IPv6Address, has_ip6: bool
) -> GLib.Variant:
    """Return the ``Config`` dictionary (tunnel device and external gateway)."""
    config = {
        NM.VPN_PLUGIN_CONFIG_TUNDEV: GLib.Variant("s", tundev),
        NM.VPN_PLUGIN_CONFIG_MTU: GLib.Variant("u", mtu),
        NM.VPN_PLUGIN_CONFIG_HAS_IP4: GLib.Variant("b", True),
        NM.VPN_PLUGIN_CONFIG_HAS_IP6: GLib.Variant("b", has_ip6),
    }
    # NetworkManager routes the external gateway via the physical device, which
    # keeps Xray's own connection to the server out of the tunnel.
    if isinstance(gateway, IPv4Address):
        config[NM.VPN_PLUGIN_CONFIG_EXT_GATEWAY] = GLib.Variant("u", ip4_to_u32(gateway))
    else:
        config[NM.VPN_PLUGIN_CONFIG_EXT_GATEWAY] = _ip6_bytes(gateway)
    return GLib.Variant("a{sv}", config)


def _split_default(family: int, networks: tuple[str, ...]) -> list[NM.IPRoute]:
    return [NM.IPRoute.new(family, network, 1, None, -1) for network in networks]


def ip4_config(options: TunnelOptions, *, split_default: bool = True) -> GLib.Variant:
    """Return the ``Ip4Config`` dictionary: tunnel address, DNS and default route.

    With *split_default* the tunnel also gets :data:`SPLIT_DEFAULT4`; leave it out
    when the profile must not get the default route (``ipv4.never-default``).
    """
    config = {
        NM.VPN_PLUGIN_IP4_CONFIG_ADDRESS: GLib.Variant("u", ip4_to_u32(TUN_ADDRESS4.ip)),
        NM.VPN_PLUGIN_IP4_CONFIG_PREFIX: GLib.Variant("u", TUN_ADDRESS4.network.prefixlen),
        NM.VPN_PLUGIN_IP4_CONFIG_DNS: GLib.Variant(
            "au", [ip4_to_u32(address) for address in options.dns]
        ),
        NM.VPN_PLUGIN_IP4_CONFIG_NEVER_DEFAULT: GLib.Variant("b", False),
    }
    if split_default:
        config[NM.VPN_PLUGIN_IP4_CONFIG_ROUTES] = NM.utils_ip4_routes_to_variant(
            _split_default(socket.AF_INET, SPLIT_DEFAULT4)
        )
    return GLib.Variant("a{sv}", config)


def ip6_config(options: TunnelOptions, *, split_default: bool = True) -> GLib.Variant:
    """Return the ``Ip6Config`` dictionary, like :func:`ip4_config`."""
    config = {
        NM.VPN_PLUGIN_IP6_CONFIG_ADDRESS: _ip6_bytes(TUN_ADDRESS6.ip),
        NM.VPN_PLUGIN_IP6_CONFIG_PREFIX: GLib.Variant("u", TUN_ADDRESS6.network.prefixlen),
        NM.VPN_PLUGIN_IP6_CONFIG_DNS: GLib.Variant(
            "aay", [address.packed for address in options.dns6]
        ),
        NM.VPN_PLUGIN_IP6_CONFIG_NEVER_DEFAULT: GLib.Variant("b", False),
    }
    if split_default:
        config[NM.VPN_PLUGIN_IP6_CONFIG_ROUTES] = NM.utils_ip6_routes_to_variant(
            _split_default(socket.AF_INET6, SPLIT_DEFAULT6)
        )
    return GLib.Variant("a{sv}", config)
