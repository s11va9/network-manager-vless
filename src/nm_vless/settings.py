# SPDX-License-Identifier: GPL-2.0-or-later
"""Mapping between :class:`VlessServer` and NetworkManager ``vpn.data``/``vpn.secrets``.

This module is pure Python so it can be shared by the privileged service, the
command line tool and the tests. See docs/connection-settings.md for the list of
keys, which is part of the plugin's public interface.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from typing import Final, TypeVar

from nm_vless.link import LinkError, VlessServer

__all__ = [
    "KNOWN_DATA_KEYS",
    "SECRET_UUID",
    "SettingsError",
    "TunnelOptions",
    "server_from_items",
    "server_to_items",
    "tunnel_from_items",
    "tunnel_to_items",
    "unknown_keys",
]

SECRET_UUID: Final = "uuid"  # noqa: S105 - key name, not a secret
#: NMSettingSecretFlags.NONE: the secret is stored by NetworkManager (root-only keyfile).
SECRET_FLAGS_SYSTEM: Final = "0"  # noqa: S105 - flag value, not a secret

#: VlessServer attribute -> vpn.data key.
SERVER_KEYS: Final = {
    "address": "address",
    "port": "port",
    "encryption": "encryption",
    "flow": "flow",
    "network": "network",
    "security": "security",
    "sni": "sni",
    "fingerprint": "fingerprint",
    "alpn": "alpn",
    "public_key": "reality-public-key",
    "short_id": "reality-short-id",
    "spider_x": "reality-spider-x",
    "path": "path",
    "host": "host",
    "service_name": "grpc-service-name",
    "mode": "mode",
    "extra": "xhttp-extra",
}

KEY_MTU: Final = "mtu"
KEY_DNS: Final = "dns"
KEY_DNS6: Final = "dns6"
KEY_IPV6: Final = "ipv6"
TUNNEL_KEYS: Final = frozenset({KEY_MTU, KEY_DNS, KEY_DNS6, KEY_IPV6})
#: Set on profiles created from a subscription: the subscription id and the server
#: key within it (see :func:`nm_vless.nmclient.server_key`). Ignored by the service.
KEY_SUBSCRIPTION: Final = "subscription"
KEY_SERVER_KEY: Final = "server-key"
SUBSCRIPTION_KEYS: Final = frozenset({KEY_SUBSCRIPTION, KEY_SERVER_KEY})

KNOWN_DATA_KEYS: Final = (
    frozenset(SERVER_KEYS.values()) | TUNNEL_KEYS | SUBSCRIPTION_KEYS | {f"{SECRET_UUID}-flags"}
)

DEFAULT_MTU: Final = 1500
DEFAULT_DNS: Final = (IPv4Address("1.1.1.1"), IPv4Address("1.0.0.1"))
DEFAULT_DNS6: Final = (IPv6Address("2606:4700:4700::1111"), IPv6Address("2606:4700:4700::1001"))
_MIN_MTU_IPV6: Final = 1280
_MIN_MTU_IPV4: Final = 576
_MAX_MTU: Final = 9000
_MAX_DNS: Final = 4
_BOOL_VALUES: Final = {
    "yes": True,
    "true": True,
    "1": True,
    "no": False,
    "false": False,
    "0": False,
}


class SettingsError(ValueError):
    """Connection settings are missing or invalid."""


@dataclass(frozen=True, slots=True)
class TunnelOptions:
    """Options of the local tunnel device, independent of the server."""

    mtu: int = DEFAULT_MTU
    dns: tuple[IPv4Address, ...] = DEFAULT_DNS
    dns6: tuple[IPv6Address, ...] = DEFAULT_DNS6
    ipv6: bool = True

    def __post_init__(self) -> None:
        min_mtu = _MIN_MTU_IPV6 if self.ipv6 else _MIN_MTU_IPV4
        if not min_mtu <= self.mtu <= _MAX_MTU:
            raise SettingsError(f"mtu must be in range {min_mtu}-{_MAX_MTU}")
        # Without a DNS server inside the tunnel, queries would go to the LAN resolver.
        if not self.dns or len(self.dns) > _MAX_DNS:
            raise SettingsError(f"dns must list 1-{_MAX_DNS} IPv4 addresses")
        if self.ipv6 and (not self.dns6 or len(self.dns6) > _MAX_DNS):
            raise SettingsError(f"dns6 must list 1-{_MAX_DNS} IPv6 addresses")


def server_to_items(server: VlessServer) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(data, secrets)`` for ``vpn.data`` and ``vpn.secrets``; empty values are omitted."""
    data: dict[str, str] = {}
    for attr, key in SERVER_KEYS.items():
        value = getattr(server, attr)
        text = ",".join(value) if isinstance(value, tuple) else str(value)
        if text:
            data[key] = text
    data[f"{SECRET_UUID}-flags"] = SECRET_FLAGS_SYSTEM
    return data, {SECRET_UUID: server.uuid}


def server_from_items(
    name: str, data: Mapping[str, str], secrets: Mapping[str, str]
) -> VlessServer:
    """Build a validated server from connection settings.

    Raises:
        SettingsError: a required key is missing or a value is invalid.
    """
    user_id = secrets.get(SECRET_UUID, "")
    if not user_id:
        raise SettingsError(f"missing secret {SECRET_UUID!r}")
    for required in ("address", "port"):
        if not data.get(required):
            raise SettingsError(f"missing key {required!r}")
    try:
        port = int(data["port"])
    except ValueError:
        raise SettingsError("port must be an integer") from None

    kwargs: dict[str, object] = {"name": name, "port": port, "uuid": user_id}
    for attr, key in SERVER_KEYS.items():
        if attr == "port" or key not in data:
            continue
        value = data[key]
        kwargs[attr] = tuple(a for a in value.split(",") if a) if attr == "alpn" else value
    try:
        return VlessServer(**kwargs)  # type: ignore[arg-type]
    except LinkError as exc:
        raise SettingsError(str(exc)) from exc


_Address = TypeVar("_Address", IPv4Address, IPv6Address)


def _parse_addresses(key: str, value: str, kind: type[_Address]) -> tuple[_Address, ...]:
    result: list[_Address] = []
    for item in value.replace(";", ",").split(","):
        if not item.strip():
            continue
        try:
            address = ipaddress.ip_address(item.strip())
        except ValueError:
            raise SettingsError(f"{key}: invalid address {item.strip()[:40]!r}") from None
        if not isinstance(address, kind):
            raise SettingsError(f"{key}: {address} is not an {kind.__name__[:4]} address")
        result.append(address)
    return tuple(result)


def tunnel_from_items(data: Mapping[str, str]) -> TunnelOptions:
    """Read tunnel options from ``vpn.data``; absent keys use the defaults."""
    kwargs: dict[str, object] = {}
    if KEY_MTU in data:
        try:
            kwargs["mtu"] = int(data[KEY_MTU])
        except ValueError:
            raise SettingsError("mtu must be an integer") from None
    if KEY_DNS in data:
        kwargs["dns"] = _parse_addresses(KEY_DNS, data[KEY_DNS], IPv4Address)
    if KEY_DNS6 in data:
        kwargs["dns6"] = _parse_addresses(KEY_DNS6, data[KEY_DNS6], IPv6Address)
    if KEY_IPV6 in data:
        try:
            kwargs["ipv6"] = _BOOL_VALUES[data[KEY_IPV6].strip().lower()]
        except KeyError:
            raise SettingsError("ipv6 must be 'yes' or 'no'") from None
    return TunnelOptions(**kwargs)  # type: ignore[arg-type]


def tunnel_to_items(options: TunnelOptions) -> dict[str, str]:
    """Return ``vpn.data`` items for options that differ from the defaults."""
    default = TunnelOptions()
    items: dict[str, str] = {}
    if options.mtu != default.mtu:
        items[KEY_MTU] = str(options.mtu)
    if options.dns != default.dns:
        items[KEY_DNS] = ",".join(map(str, options.dns))
    if options.dns6 != default.dns6:
        items[KEY_DNS6] = ",".join(map(str, options.dns6))
    if options.ipv6 != default.ipv6:
        items[KEY_IPV6] = "yes" if options.ipv6 else "no"
    return items


def unknown_keys(data: Mapping[str, str]) -> list[str]:
    """Return ``vpn.data`` keys this version does not understand, sorted."""
    return sorted(set(data) - KNOWN_DATA_KEYS)
