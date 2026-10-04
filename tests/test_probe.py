# SPDX-License-Identifier: GPL-2.0-or-later
"""``nm-vless test``: fetching URLs through a server without the TUN device."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from nm_vless import probe
from nm_vless.link import VlessServer

from .conftest import TEST_UUID


def test_probe_config_replaces_the_tun_inbound(xhttp_server: VlessServer) -> None:
    config = probe.build_probe_config(xhttp_server, connect_address="192.0.2.1", port=18080)
    assert config["inbounds"] == [
        {
            "tag": "probe-in",
            "listen": "127.0.0.1",
            "port": 18080,
            "protocol": "http",
            "settings": {},
        }
    ]
    outbound = config["outbounds"][0]  # type: ignore[index]
    assert outbound["settings"]["vnext"][0]["address"] == "192.0.2.1"
    assert config["log"]["loglevel"] == "info"  # type: ignore[index]


def _xray() -> Path | None:
    found = os.environ.get("NM_VLESS_TEST_XRAY") or shutil.which("xray")
    return Path(found) if found else None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class _Ok(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def web_server() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


@pytest.fixture
def vless_server() -> Iterator[VlessServer]:
    binary = _xray()
    if binary is None:
        pytest.skip("set NM_VLESS_TEST_XRAY to run xray")
    port = _free_port()
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": port,
                "protocol": "vless",
                "settings": {"clients": [{"id": TEST_UUID}], "decryption": "none"},
            }
        ],
        # Recent xray versions refuse private destinations unless allowed explicitly.
        "outbounds": [
            {
                "protocol": "freedom",
                "settings": {"finalRules": [{"action": "allow", "ip": ["127.0.0.0/8"]}]},
            }
        ],
    }
    proc = subprocess.Popen(
        [str(binary), "run", "-config", "stdin:"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(config))
    proc.stdin.close()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.1)
    yield VlessServer(name="Local", address="127.0.0.1", port=port, uuid=TEST_UUID)
    proc.terminate()
    proc.wait(5)


def test_probe_fetches_through_the_server(vless_server: VlessServer, web_server: str) -> None:
    xray_path = _xray()
    assert xray_path is not None
    unreachable = f"https://127.0.0.1:{_free_port()}/"
    report = probe.run_probe(
        vless_server, [web_server, unreachable], xray_path=xray_path, timeout=5, trace=False
    )
    ok, failed = report.results
    assert (ok.url, ok.ok, ok.detail) == (web_server, True, "HTTP 204")
    # The server cannot reach the destination: like in a browser, the TLS handshake
    # is cut off (net::ERR_CONNECTION_CLOSED).
    assert failed.url == unreachable
    assert not failed.ok
    assert "EOF" in failed.detail


def test_probe_reports_an_unreachable_server(web_server: str) -> None:
    xray_path = _xray()
    if xray_path is None:
        pytest.skip("set NM_VLESS_TEST_XRAY to run xray")
    server = VlessServer(name="Down", address="127.0.0.1", port=_free_port(), uuid=TEST_UUID)
    report = probe.run_probe(server, [web_server], xray_path=xray_path, timeout=5, trace=False)
    assert not report.results[0].ok
    # The client's own error (it cannot connect to the server) is shown.
    assert any("connection refused" in line for line in report.xray_messages)
