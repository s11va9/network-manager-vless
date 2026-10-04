# SPDX-License-Identifier: GPL-2.0-or-later
"""Shared fixtures. All credentials here are synthetic test values."""

from __future__ import annotations

import pytest

from nm_vless.link import VlessServer

TEST_UUID = "b831381d-6324-4d53-ad4f-8cda48b30811"
TEST_PBK = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"

REALITY_LINK = (
    f"vless://{TEST_UUID}@203.0.113.10:443?encryption=none&flow=xtls-rprx-vision"
    f"&security=reality&sni=www.example.com&fp=firefox&pbk={TEST_PBK}&sid=6ba85179e30d4fc2"
    "&spx=%2F&type=tcp&headerType=none#Reality%20%F0%9F%87%B3%F0%9F%87%B1"
)
XHTTP_LINK = (
    f"vless://{TEST_UUID}@cdn.example.org:443?encryption=none&security=tls&sni=cdn.example.org"
    "&fp=chrome&alpn=h2%2Chttp%2F1.1&type=xhttp&path=%2Fxh&mode=packet-up"
    "&extra=%7B%22xPaddingBytes%22%3A%22100-1000%22%7D#XHTTP"
)
WS_LINK = (
    f"vless://{TEST_UUID}@ws.example.net:8443?security=tls&type=ws&path=%2Fws%3Fed%3D2048"
    "&host=front.example.net#WS"
)
GRPC_LINK = (
    f"vless://{TEST_UUID}@[2001:db8::1]:443?security=tls&sni=grpc.example.net&type=grpc"
    "&serviceName=svc&mode=multi#gRPC"
)


@pytest.fixture
def reality_server() -> VlessServer:
    return VlessServer(
        name="Reality",
        address="203.0.113.10",
        port=443,
        uuid=TEST_UUID,
        flow="xtls-rprx-vision",
        security="reality",
        sni="www.example.com",
        fingerprint="chrome",
        public_key=TEST_PBK,
        short_id="6ba85179e30d4fc2",
    )


@pytest.fixture
def xhttp_server() -> VlessServer:
    return VlessServer(
        name="XHTTP",
        address="cdn.example.org",
        port=443,
        uuid=TEST_UUID,
        network="xhttp",
        security="tls",
        fingerprint="chrome",
        path="/xh",
        mode="packet-up",
    )
