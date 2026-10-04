# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address

import pytest

from nm_vless import settings
from nm_vless.link import VlessServer, parse_link

from .conftest import GRPC_LINK, REALITY_LINK, TEST_UUID, WS_LINK, XHTTP_LINK


@pytest.mark.parametrize("link", [REALITY_LINK, XHTTP_LINK, WS_LINK, GRPC_LINK])
def test_round_trip(link: str) -> None:
    server = parse_link(link)
    data, secrets = settings.server_to_items(server)
    assert settings.server_from_items(server.name, data, secrets) == server


def test_secret_is_not_in_data(reality_server: VlessServer) -> None:
    data, secrets = settings.server_to_items(reality_server)
    assert secrets == {"uuid": TEST_UUID}
    assert TEST_UUID not in data.values()
    assert data["uuid-flags"] == "0"


def test_empty_values_are_omitted(reality_server: VlessServer) -> None:
    data, _ = settings.server_to_items(reality_server)
    assert "path" not in data
    assert all(data.values())


def test_missing_secret() -> None:
    with pytest.raises(settings.SettingsError, match="missing secret 'uuid'"):
        settings.server_from_items("x", {"address": "a.example", "port": "443"}, {})


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"port": "443"}, "missing key 'address'"),
        ({"address": "a.example", "port": "https"}, "port must be an integer"),
        ({"address": "a.example", "port": "443", "network": "kcp"}, "unsupported transport"),
    ],
)
def test_invalid_data(data: dict[str, str], message: str) -> None:
    with pytest.raises(settings.SettingsError, match=message):
        settings.server_from_items("x", data, {"uuid": TEST_UUID})


def test_tunnel_defaults() -> None:
    options = settings.tunnel_from_items({})
    assert options == settings.TunnelOptions()
    assert options.mtu == 1500
    assert options.ipv6 is True
    assert settings.tunnel_to_items(options) == {}


def test_tunnel_round_trip() -> None:
    items = {"mtu": "1400", "dns": "9.9.9.9, 149.112.112.112", "dns6": "2620:fe::fe", "ipv6": "no"}
    options = settings.tunnel_from_items(items)
    assert options.mtu == 1400
    assert options.dns == (IPv4Address("9.9.9.9"), IPv4Address("149.112.112.112"))
    assert options.dns6 == (IPv6Address("2620:fe::fe"),)
    assert options.ipv6 is False
    assert settings.tunnel_from_items(settings.tunnel_to_items(options)) == options


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ({"mtu": "100"}, "mtu must be in range"),
        ({"mtu": "big"}, "mtu must be an integer"),
        ({"dns": ""}, "dns must list"),
        ({"dns": "2620:fe::fe"}, "not an IPv4 address"),
        ({"dns": "resolver.example"}, "invalid address"),
        ({"ipv6": "maybe"}, "ipv6 must be"),
    ],
)
def test_invalid_tunnel_options(items: dict[str, str], message: str) -> None:
    with pytest.raises(settings.SettingsError, match=message):
        settings.tunnel_from_items(items)


def test_unknown_keys() -> None:
    assert settings.unknown_keys({"address": "a", "mtu": "1400", "future-key": "1"}) == [
        "future-key"
    ]
