# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import dataclasses

import pytest
from gi.repository import GLib

from nm_vless import settings
from nm_vless.cli import link_to_keyfile
from nm_vless.link import LinkError, VlessServer, format_link, parse_link

from .conftest import GRPC_LINK, REALITY_LINK, TEST_PBK, TEST_UUID, WS_LINK, XHTTP_LINK


def test_parse_reality() -> None:
    server = parse_link(REALITY_LINK)
    assert server.name == "Reality 🇳🇱"
    assert server.address == "203.0.113.10"
    assert server.port == 443
    assert server.uuid == TEST_UUID
    assert server.flow == "xtls-rprx-vision"
    assert server.network == "tcp"
    assert server.security == "reality"
    assert server.sni == "www.example.com"
    assert server.fingerprint == "firefox"
    assert server.public_key == TEST_PBK
    assert server.short_id == "6ba85179e30d4fc2"
    assert server.spider_x == "/"


def test_parse_xhttp_normalizes_extra() -> None:
    server = parse_link(XHTTP_LINK)
    assert server.network == "xhttp"
    assert server.security == "tls"
    assert server.alpn == ("h2", "http/1.1")
    assert server.path == "/xh"
    assert server.mode == "packet-up"
    assert server.extra == '{"xPaddingBytes":"100-1000"}'


def test_parse_ws_keeps_query_in_path() -> None:
    server = parse_link(WS_LINK)
    assert server.path == "/ws?ed=2048"
    assert server.host == "front.example.net"
    assert server.sni == ""


def test_parse_grpc_ipv6() -> None:
    server = parse_link(GRPC_LINK)
    assert server.address == "2001:db8::1"
    assert server.endpoint == "[2001:db8::1]:443"
    assert server.service_name == "svc"
    assert server.mode == "multi"


def test_defaults_and_name_fallback() -> None:
    server = parse_link(f"vless://{TEST_UUID}@Example.COM:8080")
    assert server.name == "example.com:8080"
    assert server.address == "example.com"
    assert (server.network, server.security, server.encryption) == ("tcp", "none", "none")


def test_uuid_is_normalized() -> None:
    server = parse_link(f"vless://{TEST_UUID.upper()}@example.com:443")
    assert server.uuid == TEST_UUID


def test_custom_short_id_is_accepted() -> None:
    assert parse_link("vless://my-secret@example.com:443").uuid == "my-secret"


def test_idna_host() -> None:
    server = parse_link(f"vless://{TEST_UUID}@пример.рф:443")
    assert server.address == "xn--e1afmkfd.xn--p1ai"


def test_legacy_aliases() -> None:
    assert parse_link(f"vless://{TEST_UUID}@a.example:1?type=raw").network == "tcp"
    assert parse_link(f"vless://{TEST_UUID}@a.example:1?type=splithttp").network == "xhttp"


def test_irrelevant_parameters_are_dropped() -> None:
    server = parse_link(
        f"vless://{TEST_UUID}@a.example:443?type=tcp&security=none&path=/x&host=h.example"
        "&serviceName=s&sni=ignored.example&alpn=h2"
    )
    assert (server.path, server.host, server.service_name, server.sni, server.alpn) == (
        "",
        "",
        "",
        "",
        (),
    )


@pytest.mark.parametrize("link", [REALITY_LINK, XHTTP_LINK, WS_LINK, GRPC_LINK])
def test_round_trip(link: str) -> None:
    server = parse_link(link)
    assert parse_link(format_link(server)) == server


@pytest.mark.parametrize(
    ("link", "message"),
    [
        ("vmess://abc", "not a vless:// link"),
        ("vless://example.com:443", "missing user id"),
        (f"vless://{TEST_UUID}@example.com", "missing port"),
        (f"vless://{TEST_UUID}@example.com:99999", "malformed link"),
        (f"vless://{TEST_UUID}:pw@example.com:443", "unexpected password"),
        (f"vless://{TEST_UUID}@example.com:443?type=kcp", "unsupported transport"),
        (f"vless://{TEST_UUID}@example.com:443?security=xtls", "unsupported security"),
        (f"vless://{TEST_UUID}@example.com:443?security=tls&allowInsecure=1", "insecure TLS"),
        (f"vless://{TEST_UUID}@example.com:443?headerType=http", "headerType"),
        (f"vless://{TEST_UUID}@example.com:443?security=reality&sni=a.example", "public key"),
        (f"vless://{TEST_UUID}@example.com:443?security=reality&pbk={TEST_PBK}", "requires sni"),
        (f"vless://{TEST_UUID}@example.com:443?flow=xtls-rprx-vision", "requires tcp transport"),
        (f"vless://{TEST_UUID}@example.com:443?type=xhttp&extra=%5B1%5D", "JSON object"),
        (f"vless://{TEST_UUID}@example.com:443?type=xhttp&extra=nope", "not valid JSON"),
        (f"vless://{TEST_UUID}@example.com:443?type=xhttp&mode=bogus", "unsupported mode"),
        (f"vless://{TEST_UUID}@bad_host!:443", "not a valid host name"),
        ("vless://" + "x" * 31 + "@example.com:443", "user id"),
    ],
)
def test_invalid_links(link: str, message: str) -> None:
    with pytest.raises(LinkError, match=message):
        parse_link(link)


def test_errors_do_not_leak_the_user_id() -> None:
    link = f"vless://{TEST_UUID}@example.com:443?security=tls&allowInsecure=1"
    with pytest.raises(LinkError) as info:
        parse_link(link)
    assert TEST_UUID not in str(info.value)


def test_repr_hides_the_user_id(reality_server: VlessServer) -> None:
    assert TEST_UUID not in repr(reality_server)


def test_server_is_validated_on_construction(reality_server: VlessServer) -> None:
    with pytest.raises(LinkError, match="port"):
        dataclasses.replace(reality_server, port=0)
    with pytest.raises(LinkError, match="control characters"):
        dataclasses.replace(reality_server, name="evil\nname")
    with pytest.raises(LinkError, match="only supported with reality"):
        dataclasses.replace(reality_server, security="tls", flow="")


def test_link_to_keyfile_round_trip() -> None:
    text = link_to_keyfile(XHTTP_LINK)
    keyfile = GLib.KeyFile()
    keyfile.load_from_data(text, len(text.encode()), GLib.KeyFileFlags.NONE)
    data = {k: keyfile.get_string("vpn", k) for k in keyfile.get_keys("vpn")[0]}
    secrets = {k: keyfile.get_string("vpn-secrets", k) for k in keyfile.get_keys("vpn-secrets")[0]}
    name = keyfile.get_string("connection", "id")
    assert settings.server_from_items(name, data, secrets) == parse_link(XHTTP_LINK)
