# SPDX-License-Identifier: GPL-2.0-or-later
"""Create and manage VLESS connection profiles through libnm (unprivileged side)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final, TypeVar

import gi

gi.require_version("NM", "1.0")
from gi.repository import NM, GLib

from nm_vless import SERVICE_TYPE, settings
from nm_vless.link import VlessServer

__all__ = [
    "TUNNEL_DNS_PRIORITY",
    "USER_KEY_SERVER",
    "USER_KEY_SUBSCRIPTION",
    "NMError",
    "NMSession",
    "build_connection",
    "data_server_key",
    "has_legacy_user_data",
    "leaks_dns",
    "owned_by_subscription",
    "server_key",
    "server_key_of",
    "subscription_items",
    "subscription_of",
    "user_data",
    "vpn_data",
]

#: NMSettingUser keys that marked subscription profiles before 0.5.0. Ubuntu's
#: netplan integration loses the user setting, so they are only read, to migrate
#: profiles to the vpn.data keys in :mod:`nm_vless.settings`.
USER_KEY_SUBSCRIPTION: Final = "nm-vless.subscription"
USER_KEY_SERVER: Final = "nm-vless.server-key"

#: Negative DNS priority: only the tunnel's DNS servers are used while it is up, so
#: queries cannot leak through the physical interface (see ipv4.dns-priority in
#: nm-settings-nmcli(5)).
TUNNEL_DNS_PRIORITY: Final = -50

_T = TypeVar("_T")


class NMError(RuntimeError):
    """A NetworkManager operation failed."""


def _hash_key(parts: Iterable[str]) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def server_key(server: VlessServer) -> str:
    """Stable identity of a server inside a subscription.

    Excludes the user id and display name, so rotating credentials or renaming a
    server updates the existing profile instead of replacing it.
    """
    return _hash_key(
        (server.address, str(server.port), server.network, server.path, server.service_name)
    )


def data_server_key(data: Mapping[str, str]) -> str:
    """:func:`server_key` of the server described by a profile's ``vpn.data``."""
    keys = settings.SERVER_KEYS
    return _hash_key(
        data.get(keys[attr], default)
        for attr, default in (
            ("address", ""),
            ("port", ""),
            ("network", "tcp"),  # VlessServer.network default
            ("path", ""),
            ("service_name", ""),
        )
    )


def user_data(connection: NM.Connection, key: str) -> str | None:
    s_user = connection.get_setting(NM.SettingUser)
    return s_user.get_data(key) if s_user is not None else None


def vpn_data(connection: NM.Connection) -> dict[str, str]:
    s_vpn = connection.get_setting_vpn()
    if s_vpn is None:
        return {}
    return {key: s_vpn.get_data_item(key) for key in s_vpn.get_data_keys()}


def subscription_of(connection: NM.Connection) -> str | None:
    """Id of the subscription *connection* was created from, if any."""
    return vpn_data(connection).get(settings.KEY_SUBSCRIPTION) or user_data(
        connection, USER_KEY_SUBSCRIPTION
    )


def server_key_of(connection: NM.Connection) -> str | None:
    """Server key stored in a subscription profile, if any."""
    return vpn_data(connection).get(settings.KEY_SERVER_KEY) or user_data(
        connection, USER_KEY_SERVER
    )


def has_legacy_user_data(connection: NM.Connection) -> bool:
    """True if *connection* still carries the pre-0.5.0 NMSettingUser keys."""
    return any(user_data(connection, key) for key in (USER_KEY_SUBSCRIPTION, USER_KEY_SERVER))


def _drop_legacy_user_data(connection: NM.Connection) -> None:
    s_user = connection.get_setting(NM.SettingUser)
    if s_user is None:
        return
    for key in (USER_KEY_SUBSCRIPTION, USER_KEY_SERVER):
        s_user.set_data(key, None)
    if not s_user.get_keys():
        connection.remove_setting(NM.SettingUser)


def _prefer_tunnel_dns(connection: NM.Connection) -> None:
    """Keep DNS in the tunnel unless the user set a DNS priority of their own."""
    for setting in (connection.get_setting_ip4_config(), connection.get_setting_ip6_config()):
        if setting is not None and setting.get_dns_priority() == 0:
            setting.props.dns_priority = TUNNEL_DNS_PRIORITY


def subscription_items(subscription_id: str | None, key: str | None) -> dict[str, str]:
    """``vpn.data`` items that link a profile to a subscription."""
    items = {}
    if subscription_id is not None:
        items[settings.KEY_SUBSCRIPTION] = subscription_id
    if key is not None:
        items[settings.KEY_SERVER_KEY] = key
    return items


def build_connection(
    server: VlessServer,
    *,
    owner: str | None,
    tunnel: settings.TunnelOptions | None = None,
    subscription_id: str | None = None,
    key: str | None = None,
    base: NM.Connection | None = None,
) -> NM.SimpleConnection:
    """Return a connection profile for *server*.

    Args:
        owner: user name the profile is private to, or None for all users.
            Ignored when *base* is given (the existing permissions are kept).
        tunnel: tunnel options; None keeps the options of *base* or the defaults.
        subscription_id: subscription the profile belongs to.
        key: server key within the subscription (see :func:`server_key`).
            Both are stored in ``vpn.data``; the pre-0.5.0 user setting keys are removed.
        base: existing profile to update; its UUID, autoconnect and IP settings
            are preserved, except that the default DNS priority (0) is replaced
            with :data:`TUNNEL_DNS_PRIORITY`, as the editor does on saving.
    """
    if base is None:
        connection = NM.SimpleConnection.new()
        s_con = NM.SettingConnection(
            uuid=NM.utils_uuid_generate(), type=NM.SETTING_VPN_SETTING_NAME, autoconnect=False
        )
        if owner:
            s_con.add_permission("user", owner, None)
        connection.add_setting(s_con)
        connection.add_setting(
            NM.SettingIP4Config(
                method=NM.SETTING_IP4_CONFIG_METHOD_AUTO, dns_priority=TUNNEL_DNS_PRIORITY
            )
        )
        connection.add_setting(
            NM.SettingIP6Config(
                method=NM.SETTING_IP6_CONFIG_METHOD_AUTO, dns_priority=TUNNEL_DNS_PRIORITY
            )
        )
        tunnel_items = settings.tunnel_to_items(tunnel or settings.TunnelOptions())
    else:
        connection = NM.SimpleConnection.new_clone(base)
        _prefer_tunnel_dns(connection)
        old = vpn_data(base)
        tunnel_items = (
            settings.tunnel_to_items(tunnel)
            if tunnel is not None
            else {k: v for k, v in old.items() if k in settings.TUNNEL_KEYS}
        )
    connection.get_setting_connection().props.id = server.name

    data, secrets = settings.server_to_items(server)
    s_vpn = NM.SettingVpn(service_type=SERVICE_TYPE)
    for item_key, value in {
        **data,
        **tunnel_items,
        **subscription_items(subscription_id, key),
    }.items():
        s_vpn.add_data_item(item_key, value)
    for item_key, value in secrets.items():
        s_vpn.add_secret(item_key, value)
    connection.add_setting(s_vpn)
    _drop_legacy_user_data(connection)

    try:
        connection.verify()
    except GLib.Error as exc:
        raise NMError(f"invalid connection profile: {exc.message}") from exc
    return connection


class NMSession:
    """Synchronous facade over the asynchronous libnm client API."""

    def __init__(self) -> None:
        try:
            self._client = NM.Client.new(None)
        except GLib.Error as exc:
            raise NMError(f"cannot connect to NetworkManager: {exc.message}") from exc

    @property
    def client(self) -> NM.Client:
        return self._client

    def _call(self, start: Callable[[Callable[..., None]], None], finish: Callable[..., _T]) -> _T:
        loop = GLib.MainLoop()
        outcome: dict[str, Any] = {}

        def done(source: Any, result: Any, *_: Any) -> None:
            try:
                outcome["value"] = finish(source, result)
            except GLib.Error as exc:
                outcome["error"] = exc
            loop.quit()

        start(done)
        loop.run()
        if "error" in outcome:
            error: GLib.Error = outcome["error"]
            raise NMError(error.message) from error
        value: _T = outcome["value"]
        return value

    def vless_connections(self) -> list[NM.RemoteConnection]:
        return [
            c
            for c in self._client.get_connections()
            if c.get_setting_vpn() is not None
            and c.get_setting_vpn().get_service_type() == SERVICE_TYPE
        ]

    def find(self, name_or_uuid: str) -> NM.RemoteConnection:
        matches = [
            c for c in self.vless_connections() if name_or_uuid in (c.get_uuid(), c.get_id())
        ]
        if not matches:
            raise NMError(f"no VLESS connection named {name_or_uuid!r}")
        if len(matches) > 1:
            raise NMError(f"{name_or_uuid!r} is ambiguous, use the connection UUID")
        remote: NM.RemoteConnection = matches[0]
        return remote

    def active_uuids(self) -> set[str]:
        return {ac.get_uuid() for ac in self._client.get_active_connections()}

    def add(self, connection: NM.Connection) -> NM.RemoteConnection:
        flags = NM.SettingsAddConnection2Flags.TO_DISK
        remote, _result = self._call(
            lambda cb: self._client.add_connection2(
                connection.to_dbus(NM.ConnectionSerializationFlags.ALL),
                flags,
                None,
                False,
                None,
                cb,
            ),
            lambda client, result: client.add_connection2_finish(result),
        )
        created: NM.RemoteConnection = remote
        return created

    def update(self, remote: NM.RemoteConnection, connection: NM.Connection) -> None:
        self._call(
            lambda cb: remote.update2(
                connection.to_dbus(NM.ConnectionSerializationFlags.ALL),
                NM.SettingsUpdate2Flags.TO_DISK,
                None,
                None,
                cb,
            ),
            lambda conn, result: conn.update2_finish(result),
        )

    def delete(self, remote: NM.RemoteConnection) -> None:
        self._call(
            lambda cb: remote.delete_async(None, cb),
            lambda conn, result: conn.delete_finish(result),
        )

    def secrets(self, remote: NM.RemoteConnection) -> dict[str, str]:
        """Return the VPN secrets of *remote* (requires permission to read them)."""
        variant = self._call(
            lambda cb: remote.get_secrets_async(NM.SETTING_VPN_SETTING_NAME, None, cb),
            lambda conn, result: conn.get_secrets_finish(result),
        )
        unpacked = variant.unpack() if variant is not None else {}
        vpn = unpacked.get(NM.SETTING_VPN_SETTING_NAME, {})
        return dict(vpn.get("secrets", {}))


def leaks_dns(connection: NM.Connection) -> bool:
    """True if the profile's DNS priority lets other interfaces answer queries."""
    s_ip4 = connection.get_setting_ip4_config()
    return s_ip4 is None or s_ip4.get_dns_priority() >= 0


def owned_by_subscription(
    connections: Iterable[NM.RemoteConnection], subscription_id: str
) -> list[NM.RemoteConnection]:
    return [c for c in connections if subscription_of(c) == subscription_id]
