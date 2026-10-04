# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests that use libnm objects offline; no running NetworkManager is required."""

from __future__ import annotations

import dataclasses
import socket
import struct
import sys
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Any

import gi
import pytest

gi.require_version("NM", "1.0")
from gi.repository import NM

from nm_vless import SERVICE_TYPE, settings, vpnconfig
from nm_vless.link import VlessServer
from nm_vless.nmclient import (
    TUNNEL_DNS_PRIORITY,
    USER_KEY_SERVER,
    USER_KEY_SUBSCRIPTION,
    build_connection,
    data_server_key,
    leaks_dns,
    server_key,
    server_key_of,
    subscription_of,
    user_data,
    vpn_data,
)
from nm_vless.sync import sync_subscription

from .conftest import TEST_UUID


def test_build_new_connection(reality_server: VlessServer) -> None:
    conn = build_connection(reality_server, owner="alice")
    s_con = conn.get_setting_connection()
    assert s_con.get_id() == "Reality"
    assert s_con.get_connection_type() == "vpn"
    assert s_con.get_autoconnect() is False
    assert s_con.props.permissions == ["user:alice:"]
    s_vpn = conn.get_setting_vpn()
    assert s_vpn.get_service_type() == SERVICE_TYPE
    assert s_vpn.get_secret("uuid") == TEST_UUID
    assert vpn_data(conn)["uuid-flags"] == str(int(NM.SettingSecretFlags.NONE))
    assert vpn_data(conn)["security"] == "reality"
    assert conn.get_setting(NM.SettingUser) is None


def test_build_shared_connection_with_tunnel_options(reality_server: VlessServer) -> None:
    tunnel = settings.TunnelOptions(mtu=1400, ipv6=False)
    conn = build_connection(reality_server, owner=None, tunnel=tunnel)
    assert conn.get_setting_connection().props.permissions == []
    assert vpn_data(conn)["mtu"] == "1400"
    assert vpn_data(conn)["ipv6"] == "no"


def test_update_keeps_identity_and_user_choices(
    reality_server: VlessServer, xhttp_server: VlessServer
) -> None:
    original = build_connection(
        reality_server, owner="alice", tunnel=settings.TunnelOptions(mtu=1400)
    )
    original.get_setting_connection().props.autoconnect = True
    updated = build_connection(
        xhttp_server, owner="bob", subscription_id="sub1", key="k1", base=original
    )
    s_con = updated.get_setting_connection()
    assert s_con.get_uuid() == original.get_setting_connection().get_uuid()
    assert s_con.get_id() == "XHTTP"
    assert s_con.get_autoconnect() is True
    assert s_con.props.permissions == ["user:alice:"]
    assert vpn_data(updated)["mtu"] == "1400"
    assert vpn_data(updated)["network"] == "xhttp"
    assert "reality-public-key" not in vpn_data(updated)
    assert vpn_data(updated)["subscription"] == "sub1"
    assert vpn_data(updated)["server-key"] == "k1"
    assert subscription_of(updated) == "sub1"
    assert server_key_of(updated) == "k1"
    # netplan loses the user setting (see docs/architecture.md), so it is not used.
    assert updated.get_setting(NM.SettingUser) is None
    assert not settings.unknown_keys(vpn_data(updated))


def test_legacy_user_data_is_read_and_replaced(reality_server: VlessServer) -> None:
    legacy = build_connection(reality_server, owner=None)
    s_user = NM.SettingUser()
    s_user.set_data(USER_KEY_SUBSCRIPTION, "sub1")
    s_user.set_data(USER_KEY_SERVER, "k1")
    s_user.set_data("other.key", "kept")
    legacy.add_setting(s_user)
    assert subscription_of(legacy) == "sub1"
    assert server_key_of(legacy) == "k1"

    updated = build_connection(
        reality_server, owner=None, subscription_id="sub1", key="k1", base=legacy
    )
    assert user_data(updated, USER_KEY_SUBSCRIPTION) is None
    assert user_data(updated, "other.key") == "kept"
    assert subscription_of(updated) == "sub1"


def test_data_server_key_matches_server_key(
    reality_server: VlessServer, xhttp_server: VlessServer
) -> None:
    for server in (reality_server, xhttp_server):
        data, _secrets = settings.server_to_items(server)
        assert data_server_key(data) == server_key(server)


def test_server_key_ignores_credentials_and_name(reality_server: VlessServer) -> None:
    renamed = dataclasses.replace(
        reality_server, name="Other", uuid="00000000-0000-4000-8000-000000000000"
    )
    assert server_key(renamed) == server_key(reality_server)
    assert server_key(dataclasses.replace(reality_server, port=8443)) != server_key(reality_server)


def test_gateway_encoding_matches_network_manager() -> None:
    address = IPv4Address("203.0.113.10")
    # NetworkManager reads the u32 natively and uses its memory as in_addr_t.
    expected = struct.unpack("=I", socket.inet_aton(str(address)))[0]
    assert vpnconfig.ip4_to_u32(address) == expected
    assert sys.byteorder in ("little", "big")


def test_generic_config_ipv4_gateway() -> None:
    variant = vpnconfig.generic_config(
        tundev="vless0", mtu=1400, gateway=IPv4Address("203.0.113.10"), has_ip6=True
    )
    assert variant.get_type_string() == "a{sv}"
    config = variant.unpack()
    assert config["tundev"] == "vless0"
    assert config["mtu"] == 1400
    assert config["has-ip4"] is True
    assert config["has-ip6"] is True
    assert config["gateway"] == vpnconfig.ip4_to_u32(IPv4Address("203.0.113.10"))


def test_generic_config_ipv6_gateway() -> None:
    gateway = IPv6Address("2001:db8::1")
    variant = vpnconfig.generic_config(tundev="vless0", mtu=1500, gateway=gateway, has_ip6=False)
    assert variant.lookup_value("gateway", None).get_type_string() == "ay"
    assert bytes(variant.unpack()["gateway"]) == gateway.packed


def test_ip_configs() -> None:
    options = settings.TunnelOptions()
    ip4_variant = vpnconfig.ip4_config(options)
    assert ip4_variant.lookup_value("routes", None).get_type_string() == "aau"
    ip4: dict[str, Any] = ip4_variant.unpack()
    assert ip4["address"] == vpnconfig.ip4_to_u32(vpnconfig.TUN_ADDRESS4.ip)
    assert ip4["prefix"] == 30
    assert ip4["dns"] == [vpnconfig.ip4_to_u32(a) for a in options.dns]
    assert ip4["never-default"] is False

    ip6_variant = vpnconfig.ip6_config(options)
    assert ip6_variant.lookup_value("dns", None).get_type_string() == "aay"
    ip6: dict[str, Any] = ip6_variant.unpack()
    assert bytes(ip6["address"]) == vpnconfig.TUN_ADDRESS6.ip.packed
    assert ip6["prefix"] == 64
    assert [bytes(a) for a in ip6["dns"]] == [a.packed for a in options.dns6]


def test_tunnel_routes_both_halves_of_the_address_space() -> None:
    options = settings.TunnelOptions()
    routes4 = vpnconfig.ip4_config(options).unpack()["routes"]
    # [destination, prefix, next hop, metric]; a zero next hop means "on the device".
    assert sorted(routes4) == sorted(
        [vpnconfig.ip4_to_u32(IPv4Address(a)), 1, 0, 0] for a in vpnconfig.SPLIT_DEFAULT4
    )
    routes6 = vpnconfig.ip6_config(options).unpack()["routes"]
    assert sorted((IPv6Address(bytes(dest)), prefix) for dest, prefix, *_ in routes6) == [
        (IPv6Address("::"), 1),
        (IPv6Address("8000::"), 1),
    ]

    for config in (
        vpnconfig.ip4_config(options, split_default=False),
        vpnconfig.ip6_config(options, split_default=False),
    ):
        assert config.lookup_value("routes", None) is None


class FakeStore:
    """In-memory stand-in for NMSession; SimpleConnection plays NM.RemoteConnection."""

    def __init__(self) -> None:
        self.remotes: list[NM.SimpleConnection] = []
        self.active: set[str] = set()
        self.calls: list[str] = []

    def vless_connections(self) -> list[NM.SimpleConnection]:
        return list(self.remotes)

    def active_uuids(self) -> set[str]:
        return self.active

    def add(self, connection: NM.SimpleConnection) -> NM.SimpleConnection:
        self.calls.append("add")
        self.remotes.append(connection)
        return connection

    def update(self, remote: NM.SimpleConnection, connection: NM.SimpleConnection) -> None:
        self.calls.append("update")
        remote.replace_settings_from_connection(connection)

    def delete(self, remote: NM.SimpleConnection) -> None:
        self.calls.append("delete")
        self.remotes.remove(remote)

    def secrets(self, remote: NM.SimpleConnection) -> dict[str, str]:
        s_vpn = remote.get_setting_vpn()
        return {k: s_vpn.get_secret(k) for k in s_vpn.get_secret_keys()}


def test_sync_add_update_remove(reality_server: VlessServer, xhttp_server: VlessServer) -> None:
    store = FakeStore()
    report = sync_subscription(store, "s1", [reality_server, xhttp_server], owner="alice")
    assert report.added == ["Reality", "XHTTP"]
    assert len(store.remotes) == 2

    report = sync_subscription(store, "s1", [reality_server, xhttp_server], owner="alice")
    assert report.unchanged == ["Reality", "XHTTP"]
    assert store.calls == ["add", "add"]

    rotated = dataclasses.replace(reality_server, uuid="00000000-0000-4000-8000-000000000000")
    report = sync_subscription(store, "s1", [rotated], owner="alice")
    assert report.updated == ["Reality"]
    assert report.removed == ["XHTTP"]
    assert [r.get_id() for r in store.remotes] == ["Reality"]


def test_sync_never_deletes_active_or_foreign(
    reality_server: VlessServer, xhttp_server: VlessServer
) -> None:
    store = FakeStore()
    sync_subscription(store, "s1", [reality_server, xhttp_server], owner=None)
    store.add(build_connection(reality_server, owner=None))  # manually imported
    store.active = {store.remotes[1].get_uuid()}  # XHTTP is connected

    report = sync_subscription(store, "s1", [reality_server], owner=None)
    assert report.kept_active == ["XHTTP"]
    assert report.removed == []
    assert len(store.remotes) == 3


def test_sync_refuses_empty_subscription() -> None:
    with pytest.raises(ValueError, match="nothing was changed"):
        sync_subscription(FakeStore(), "s1", [], owner=None)


def test_sync_handles_duplicate_servers(reality_server: VlessServer) -> None:
    twin = dataclasses.replace(reality_server, name="Reality 2")
    store = FakeStore()
    report = sync_subscription(store, "s1", [reality_server, twin], owner=None)
    assert report.added == ["Reality", "Reality 2"]
    keys = {server_key_of(r) for r in store.remotes}
    assert len(keys) == 2


def _unmarked_copy(server: VlessServer, timestamp: int = 0) -> NM.SimpleConnection:
    """A subscription profile whose mark netplan lost (versions before 0.5.0)."""
    copy = build_connection(server, owner=None)
    copy.get_setting_connection().props.timestamp = timestamp
    return copy


def test_sync_adopts_profiles_that_lost_their_mark(
    reality_server: VlessServer, xhttp_server: VlessServer
) -> None:
    store = FakeStore()
    copies = [_unmarked_copy(reality_server, t) for t in (10, 30, 20)]
    store.remotes.extend([*copies, _unmarked_copy(xhttp_server)])
    manual = build_connection(dataclasses.replace(reality_server, name="My server"), owner=None)
    store.remotes.append(manual)

    report = sync_subscription(store, "s1", [reality_server], owner=None)
    # The most recently used copy becomes the subscription profile, the others go.
    assert report.added == []
    assert report.updated == ["Reality"]
    assert report.removed == ["Reality", "Reality"]
    assert store.remotes == [copies[1], _find(store, "XHTTP"), manual]
    assert subscription_of(copies[1]) == "s1"
    assert subscription_of(manual) is None

    report = sync_subscription(store, "s1", [reality_server], owner=None)
    assert report.unchanged == ["Reality"]
    assert len(store.remotes) == 3


def test_sync_keeps_the_active_copy(reality_server: VlessServer) -> None:
    store = FakeStore()
    copies = [_unmarked_copy(reality_server, t) for t in (30, 10)]
    store.remotes.extend(copies)
    store.active = {copies[1].get_uuid()}

    report = sync_subscription(store, "s1", [reality_server], owner=None)
    assert report.removed == ["Reality"]
    assert store.remotes == [copies[1]]
    assert subscription_of(copies[1]) == "s1"


def test_sync_removes_duplicate_marked_profiles(reality_server: VlessServer) -> None:
    store = FakeStore()
    key = server_key(reality_server)
    for _ in range(2):
        store.add(build_connection(reality_server, owner=None, subscription_id="s1", key=key))

    report = sync_subscription(store, "s1", [reality_server], owner=None)
    assert report.unchanged == ["Reality"]
    assert report.removed == ["Reality"]
    assert len(store.remotes) == 1


def test_sync_migrates_legacy_marks(reality_server: VlessServer) -> None:
    legacy = build_connection(reality_server, owner=None)
    s_user = NM.SettingUser()
    s_user.set_data(USER_KEY_SUBSCRIPTION, "s1")
    s_user.set_data(USER_KEY_SERVER, server_key(reality_server))
    legacy.add_setting(s_user)
    store = FakeStore()
    store.remotes.append(legacy)

    report = sync_subscription(store, "s1", [reality_server], owner=None)
    assert report.updated == ["Reality"]
    assert legacy.get_setting(NM.SettingUser) is None
    assert vpn_data(legacy)["subscription"] == "s1"


def _find(store: FakeStore, name: str) -> NM.SimpleConnection:
    return next(r for r in store.remotes if r.get_id() == name)


def test_new_connections_keep_dns_in_the_tunnel(reality_server: VlessServer) -> None:
    conn = build_connection(reality_server, owner=None)
    assert conn.get_setting_ip4_config().get_dns_priority() == TUNNEL_DNS_PRIORITY < 0
    assert conn.get_setting_ip6_config().get_dns_priority() == TUNNEL_DNS_PRIORITY
    assert not leaks_dns(conn)

    conn.get_setting_ip4_config().props.dns_priority = 0
    assert leaks_dns(conn)
    # Updates move profiles created before 0.4.0 (NetworkManager's default 0) into
    # the tunnel, but keep a priority the user chose.
    updated = build_connection(reality_server, owner=None, base=conn)
    assert updated.get_setting_ip4_config().get_dns_priority() == TUNNEL_DNS_PRIORITY
    assert not leaks_dns(updated)

    conn.get_setting_ip4_config().props.dns_priority = 100
    updated = build_connection(reality_server, owner=None, base=conn)
    assert updated.get_setting_ip4_config().get_dns_priority() == 100


def test_c_dns_priority_matches_python() -> None:
    header = (Path(__file__).parent.parent / "src/editor/nm-vless-common.h").read_text()
    assert f"#define NM_VLESS_DNS_PRIORITY  ({TUNNEL_DNS_PRIORITY})" in header
