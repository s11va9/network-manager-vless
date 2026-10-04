# SPDX-License-Identifier: GPL-2.0-or-later
"""Test a server without connecting the VPN.

``nm-vless test`` starts xray as the current user with a local HTTP proxy inbound
instead of the TUN device, fetches a few URLs through it and collects xray's
messages. Routes, DNS and other connections are not touched, so a profile can be
checked while another VPN is up, and failures show which destinations the server
does not forward.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from nm_vless import xray
from nm_vless.link import VlessServer

__all__ = ["DEFAULT_URLS", "TRACE_URL", "ProbeReport", "UrlResult", "run_probe"]

#: Fetched by default; the trace shows the IP address and country traffic leaves from.
TRACE_URL: Final = "https://www.cloudflare.com/cdn-cgi/trace"
DEFAULT_URLS: Final = (
    "https://www.google.com/generate_204",
    "https://www.youtube.com/",
    "https://github.com/",
)
_READY_TIMEOUT_S: Final = 10
_STOP_TIMEOUT_S: Final = 3
_SAFE_PATH: Final = "/usr/sbin:/usr/bin:/sbin:/bin"
_MAX_LINES: Final = 200
_PROXY_ERRORS: Final = frozenset({502, 503, 504})


@dataclass(frozen=True, slots=True)
class UrlResult:
    url: str
    ok: bool
    detail: str  # "HTTP 200" or the error
    seconds: float


@dataclass(slots=True)
class ProbeReport:
    results: list[UrlResult] = field(default_factory=list)
    #: ``ip`` and ``loc`` from the trace, if it could be fetched.
    exit_ip: str = ""
    exit_country: str = ""
    #: xray messages at warning level or above, or about failed connections.
    xray_messages: list[str] = field(default_factory=list)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _resolve(host: str) -> str:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise xray.XrayError(f"cannot resolve {host}: {exc}") from None
    return str(infos[0][4][0])


def build_probe_config(
    server: VlessServer, *, connect_address: str, port: int
) -> dict[str, object]:
    """The connection's xray configuration with a local HTTP proxy instead of the TUN device."""
    config = xray.build_config(
        server, connect_address=connect_address, tun_name="unused", mtu=1500, log_level="info"
    )
    config["inbounds"] = [
        {
            "tag": "probe-in",
            "listen": "127.0.0.1",
            "port": port,
            "protocol": "http",
            "settings": {},
        }
    ]
    return config


def _interesting(line: str) -> bool:
    return any(mark in line for mark in ("[Error]", "[Warning]", "failed", "rejected"))


class _Xray:
    def __init__(self, path: Path, config: dict[str, object]) -> None:
        self.lines: list[str] = []
        self._ready = threading.Event()
        self._proc = subprocess.Popen(
            [str(path), "run", "-config", "stdin:"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={"PATH": _SAFE_PATH},
            text=True,
            errors="replace",
        )
        if self._proc.stdin is None or self._proc.stdout is None:  # pragma: no cover
            raise xray.XrayError("cannot talk to xray")
        self._stdout = self._proc.stdout
        self._proc.stdin.write(json.dumps(config))
        self._proc.stdin.close()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        for raw in self._stdout:
            line = raw.rstrip()
            if len(self.lines) < _MAX_LINES:
                self.lines.append(line)
            if xray.READY_RE.search(line):
                self._ready.set()
        self._ready.set()  # exited

    def wait_ready(self) -> None:
        if not self._ready.wait(_READY_TIMEOUT_S) or self._proc.poll() is not None:
            tail = self.lines[-1] if self.lines else "no output"
            raise xray.XrayError(f"xray did not start: {tail}")

    def stop(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        self._reader.join(_STOP_TIMEOUT_S)
        self._stdout.close()


def _fetch(proxy: str, url: str, timeout: float) -> tuple[UrlResult, bytes]:
    start = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": "nm-vless-test"})  # noqa: S310
    # Set the proxy on the request: ProxyHandler would skip hosts listed in $no_proxy.
    request.set_proxy(proxy, "http")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            body: bytes = response.read(64 * 1024)
            detail = f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        # The site answered through the tunnel, so any status means it is reachable;
        # for plain http:// the local proxy itself answers 503 if the server failed.
        reached = request.type == "https" or exc.code not in _PROXY_ERRORS
        return UrlResult(url, reached, f"HTTP {exc.code}", time.monotonic() - start), b""
    except urllib.error.URLError as exc:
        return UrlResult(url, False, str(exc.reason), time.monotonic() - start), b""
    except (TimeoutError, OSError) as exc:
        return UrlResult(url, False, str(exc) or type(exc).__name__, time.monotonic() - start), b""
    return UrlResult(url, True, detail, time.monotonic() - start), body


def run_probe(
    server: VlessServer,
    urls: Sequence[str] = DEFAULT_URLS,
    *,
    xray_path: Path | None = None,
    timeout: float = 15.0,
    trace: bool = True,
) -> ProbeReport:
    """Fetch *urls*, and :data:`TRACE_URL` if *trace*, through *server*."""
    port = _free_port()
    config = build_probe_config(server, connect_address=_resolve(server.address), port=port)
    process = _Xray(xray_path or xray.find_xray(), config)
    report = ProbeReport()
    try:
        process.wait_ready()
        proxy = f"127.0.0.1:{port}"
        if trace:
            result, body = _fetch(proxy, TRACE_URL, timeout)
            for line in body.decode("utf-8", "replace").splitlines():
                key, _, value = line.partition("=")
                if key == "ip":
                    report.exit_ip = value
                elif key == "loc":
                    report.exit_country = value
            report.results.append(result)
        report.results.extend(_fetch(proxy, url, timeout)[0] for url in urls)
    finally:
        process.stop()
    report.xray_messages = [line for line in process.lines if _interesting(line)]
    return report
