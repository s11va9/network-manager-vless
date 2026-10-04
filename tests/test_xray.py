# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from nm_vless import xray
from nm_vless.link import VlessServer, parse_link

from .conftest import GRPC_LINK, REALITY_LINK, TEST_UUID, WS_LINK, XHTTP_LINK


def _build(server: VlessServer, address: str = "198.51.100.7") -> dict:  # type: ignore[type-arg]
    return xray.build_config(server, connect_address=address, tun_name="vless0", mtu=1500)


def _stream(server: VlessServer) -> dict:  # type: ignore[type-arg]
    stream: dict = _build(server)["outbounds"][0]["streamSettings"]  # type: ignore[type-arg]
    return stream


def test_tun_inbound_and_outbound(reality_server: VlessServer) -> None:
    config = _build(reality_server)
    assert config["inbounds"] == [
        {
            "tag": "tun-in",
            "port": 0,
            "protocol": "tun",
            "settings": {"name": "vless0", "mtu": 1500},
            "sniffing": {
                "enabled": True,
                "destOverride": ["http", "tls", "quic"],
                "routeOnly": False,
            },
        }
    ]
    (outbound,) = config["outbounds"]
    assert outbound["protocol"] == "vless"
    (vnext,) = outbound["settings"]["vnext"]
    assert vnext == {
        "address": "198.51.100.7",
        "port": 443,
        "users": [{"id": TEST_UUID, "encryption": "none", "flow": "xtls-rprx-vision"}],
    }
    # Xray never configures routes itself and no socket is bound to an interface.
    assert "sockopt" not in outbound["streamSettings"]
    assert "routing" not in config


def test_reality_settings(reality_server: VlessServer) -> None:
    assert _stream(reality_server)["realitySettings"] == {
        "serverName": "www.example.com",
        "fingerprint": "chrome",
        "publicKey": reality_server.public_key,
        "shortId": "6ba85179e30d4fc2",
        "spiderX": "",
    }


def test_domain_is_kept_for_sni_and_host(xhttp_server: VlessServer) -> None:
    stream = _stream(xhttp_server)
    assert stream["tlsSettings"] == {"serverName": "cdn.example.org", "fingerprint": "chrome"}
    assert stream["xhttpSettings"] == {
        "path": "/xh",
        "host": "cdn.example.org",
        "mode": "packet-up",
    }


def test_xhttp_extra_is_an_object() -> None:
    stream = _stream(parse_link(XHTTP_LINK))
    assert stream["xhttpSettings"]["extra"] == {"xPaddingBytes": "100-1000"}
    assert stream["tlsSettings"]["alpn"] == ["h2", "http/1.1"]


def test_ws_explicit_host() -> None:
    stream = _stream(parse_link(WS_LINK))
    assert stream["wsSettings"] == {"path": "/ws?ed=2048", "host": "front.example.net"}
    assert stream["tlsSettings"] == {"serverName": "ws.example.net"}


def test_grpc() -> None:
    stream = _stream(parse_link(GRPC_LINK))
    assert stream["grpcSettings"] == {"serviceName": "svc", "multiMode": True}
    assert stream["tlsSettings"] == {"serverName": "grpc.example.net"}


def test_ip_server_without_sni_has_no_server_name() -> None:
    server = parse_link(f"vless://{TEST_UUID}@192.0.2.1:443?security=tls")
    assert _stream(server)["tlsSettings"] == {}


def test_connect_address_must_be_ip(reality_server: VlessServer) -> None:
    with pytest.raises(xray.XrayError):
        _build(reality_server, address="example.com")


def test_ready_line() -> None:
    assert xray.READY_RE.search("2026/09/28 [Warning] core: Xray 26.7.28 started")
    assert not xray.READY_RE.search("Xray 26.7.28 (Xray, Penetrates Everything.) 5ca6f4b")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Xray 26.7.28 (Xray, Penetrates Everything.) 5ca6f4b (go1.26.5 linux/amd64)", (26, 7, 28)),
        ("Xray 25.12.8 (Xray, Penetrates Everything.)", (25, 12, 8)),
        ("V2Ray 5.1.0", None),
        ("", None),
    ],
)
def test_parse_version(line: str, expected: tuple[int, int, int] | None) -> None:
    assert xray.parse_version(line) == expected


def test_minimum_version_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        xray, "version", lambda _path, **_kw: "Xray 26.3.27 (Xray, Penetrates Everything.)"
    )
    with pytest.raises(xray.XrayError, match=r"26\.6\.22 or newer"):
        xray.ensure_supported("/usr/local/bin/xray")
    monkeypatch.setattr(
        xray, "version", lambda _path, **_kw: "Xray 26.6.22 (Xray, Penetrates Everything.)"
    )
    assert xray.ensure_supported("/usr/local/bin/xray").startswith("Xray 26.6.22")


def test_find_xray_prefers_environment(tmp_path: Path) -> None:
    fake = tmp_path / "xray"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    # tmp_path is owned by the test user, so the ownership check must reject it.
    with pytest.raises(xray.XrayError, match="owned by root"):
        xray.find_xray({xray.ENV_XRAY: str(fake)})


def test_find_xray_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xray, "DEFAULT_CANDIDATES", ("/nonexistent/xray",))
    with pytest.raises(xray.XrayError, match="not found"):
        xray.find_xray({})


def test_check_binary_accepts_system_binary() -> None:
    sh = Path("/bin/sh").resolve()
    info = sh.stat()
    if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        pytest.skip("/bin/sh is not a root-owned binary on this system")
    assert xray.check_binary("/bin/sh") == sh


def _xray_for_tests() -> str | None:
    return os.environ.get("NM_VLESS_TEST_XRAY") or shutil.which("xray")


@pytest.mark.skipif(_xray_for_tests() is None, reason="set NM_VLESS_TEST_XRAY to validate configs")
@pytest.mark.parametrize("link", [REALITY_LINK, XHTTP_LINK, WS_LINK, GRPC_LINK])
def test_config_is_accepted_by_xray(link: str) -> None:
    binary = _xray_for_tests()
    assert binary is not None
    config = json.dumps(_build(parse_link(link)))
    result = subprocess.run(
        [binary, "run", "-test", "-config", "stdin:"],
        input=config,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_version_error_includes_output(tmp_path: Path) -> None:
    fake = tmp_path / "xray"
    fake.write_text(
        '#!/bin/sh\necho "setpriv: setgroups failed: Operation not permitted" >&2\nexit 127\n'
    )
    fake.chmod(0o755)
    with pytest.raises(xray.XrayError, match="setgroups failed: Operation not permitted"):
        xray.version(fake)
