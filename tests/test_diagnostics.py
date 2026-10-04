# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from nm_vless.diagnostics import (
    DefaultRoute,
    competing_tunnels,
    env_local_proxies,
    local_proxy,
    own_devices,
)

TUNS = {"happ-xray", "vless0", "tun0"}


def _is_tun(dev: str) -> bool:
    return dev in TUNS


def test_foreign_tunnel_with_better_metric_is_reported() -> None:
    # A VLESS tunnel without its half-default routes (versions before 0.5.0).
    routes = [
        DefaultRoute("happ-xray", 1, 4),
        DefaultRoute("vless0", 50, 4),
        DefaultRoute("wlp3s0", 600, 4),
    ]
    assert own_devices(routes) == {"vless0"}
    assert competing_tunnels(routes, own_devices(routes), _is_tun) == [routes[0]]


def test_half_default_routes_win_over_any_default_route() -> None:
    routes = [
        DefaultRoute("happ-xray", 1, 4),
        DefaultRoute("vless0", 50, 4),
        DefaultRoute("vless0", 50, 4, prefix=1),
    ]
    assert competing_tunnels(routes, own_devices(routes), _is_tun) == []
    foreign_half = DefaultRoute("tun0", 0, 4, prefix=1)
    assert competing_tunnels([*routes, foreign_half], {"vless0"}, _is_tun) == [foreign_half]


def test_foreign_tunnel_with_worse_metric_is_ignored() -> None:
    routes = [DefaultRoute("vless0", 50, 4), DefaultRoute("tun0", 100, 4)]
    assert competing_tunnels(routes, own_devices(routes), _is_tun) == []


def test_without_vless_only_half_default_routes_are_reported() -> None:
    routes = [
        DefaultRoute("happ-xray", 1, 4),
        DefaultRoute("tun0", 0, 4, prefix=1),
        DefaultRoute("wlp3s0", 600, 4),
    ]
    assert competing_tunnels(routes, set(), _is_tun) == [routes[1]]


def test_families_are_compared_separately() -> None:
    routes = [DefaultRoute("vless0", 50, 4), DefaultRoute("tun0", 1, 6)]
    assert competing_tunnels(routes, {"vless0"}, _is_tun) == [routes[1]]


def test_local_proxy() -> None:
    settings = {"mode": "manual", "http.host": "127.0.0.1", "http.port": 10809, "https.host": ""}
    assert local_proxy(settings) == "127.0.0.1:10809"
    assert local_proxy({**settings, "mode": "none"}) is None
    assert local_proxy({"mode": "manual", "socks.host": "localhost", "socks.port": 1080}) == (
        "localhost:1080"
    )
    assert (
        local_proxy({"mode": "manual", "http.host": "proxy.corp.example", "http.port": 3128})
        is None
    )


def test_env_local_proxies() -> None:
    environ = {
        "HTTPS_PROXY": "http://127.0.0.1:10809",
        "http_proxy": "localhost:3128",
        "ALL_PROXY": "socks5://[::1]:1080",
        "NO_PROXY": "localhost,127.0.0.1",
        "FTP_PROXY": "http://127.0.0.1:21",
        "HOME": "/home/user",
    }
    assert env_local_proxies(environ) == ["ALL_PROXY", "HTTPS_PROXY", "http_proxy"]
    assert env_local_proxies({"https_proxy": "http://proxy.corp.example:3128"}) == []
    assert env_local_proxies({"https_proxy": ""}) == []
