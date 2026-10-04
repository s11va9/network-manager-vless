# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

from pathlib import Path
from typing import Any

import gi
import pytest

gi.require_version("NM", "1.0")
gi.require_version("Gio", "2.0")
from gi.repository import NM, Gio

from nm_vless.link import VlessServer
from nm_vless.nmclient import build_connection
from nm_vless.proxyguard import GnomeProxy, ProxyGuard, vless_active

HAPP = {"mode": "manual", "http.host": "127.0.0.1", "http.port": 10809}


class FakeProxy:
    """Settings that, like GSettings, report changes synchronously."""

    def __init__(self, **values: object) -> None:
        self.state: dict[str, object] = dict(values)
        self.guard: ProxyGuard | None = None
        self.modes: list[str] = []

    def mode(self) -> object:
        return self.state["mode"]

    def values(self) -> dict[str, object]:
        return dict(self.state)

    def set_mode(self, mode: str) -> None:
        self.modes.append(mode)
        self.state["mode"] = mode
        if self.guard is not None:
            self.guard.check()


def _guard(tmp_path: Path, proxy: FakeProxy) -> tuple[ProxyGuard, list[str]]:
    notes: list[str] = []
    guard = ProxyGuard(proxy, tmp_path / "state", notify=lambda summary, body: notes.append(body))
    proxy.guard = guard
    return guard, notes


def test_local_proxy_is_off_while_vless_is_up(tmp_path: Path) -> None:
    proxy = FakeProxy(**HAPP)
    guard, notes = _guard(tmp_path, proxy)

    guard.set_vless_up(False)
    assert proxy.modes == []

    guard.set_vless_up(True)
    assert proxy.mode() == "none"
    assert len(notes) == 1
    assert "127.0.0.1:10809" in notes[0]

    guard.set_vless_up(False)
    assert proxy.mode() == "manual"
    assert proxy.modes == ["none", "manual"]
    assert not (tmp_path / "state").exists()


def test_other_proxies_are_left_alone(tmp_path: Path) -> None:
    proxy = FakeProxy(mode="manual", **{"http.host": "proxy.example.com", "http.port": 3128})
    guard, notes = _guard(tmp_path, proxy)
    guard.set_vless_up(True)
    guard.set_vless_up(False)
    assert proxy.modes == []
    assert notes == []


def test_proxy_set_again_while_up_is_turned_off_again(tmp_path: Path) -> None:
    proxy = FakeProxy(**HAPP)
    guard, _notes = _guard(tmp_path, proxy)
    guard.set_vless_up(True)

    proxy.state["mode"] = "manual"  # the other client sets it again
    guard.check()
    assert proxy.mode() == "none"

    guard.set_vless_up(False)
    assert proxy.mode() == "manual"


def test_user_changes_are_not_overwritten(tmp_path: Path) -> None:
    proxy = FakeProxy(**HAPP)
    guard, _notes = _guard(tmp_path, proxy)
    guard.set_vless_up(True)

    proxy.state["mode"] = "auto"  # the user picks a PAC file meanwhile
    guard.set_vless_up(False)
    assert proxy.mode() == "auto"
    assert not (tmp_path / "state").exists()


def test_mode_is_restored_after_a_restart(tmp_path: Path) -> None:
    proxy = FakeProxy(**HAPP)
    guard, _notes = _guard(tmp_path, proxy)
    guard.set_vless_up(True)

    # The guard was stopped without restoring; a new one finds VLESS down.
    proxy.guard, _ = _guard(tmp_path, proxy)
    proxy.guard.set_vless_up(False)
    assert proxy.mode() == "manual"


def test_stop_restores_the_proxy(tmp_path: Path) -> None:
    proxy = FakeProxy(**HAPP)
    guard, _notes = _guard(tmp_path, proxy)
    guard.set_vless_up(True)
    guard.stop()
    assert proxy.mode() == "manual"


def test_invalid_state_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "state").write_text("rm -rf\n")
    proxy = FakeProxy(mode="none")
    guard, _notes = _guard(tmp_path, proxy)
    guard.set_vless_up(False)
    assert proxy.modes == []


def test_gnome_proxy_reads_and_writes_gsettings() -> None:
    if not GnomeProxy.available():
        pytest.skip("org.gnome.system.proxy schema is not installed")
    backend = Gio.memory_settings_backend_new()
    http = Gio.Settings.new_with_backend("org.gnome.system.proxy.http", backend)
    http.set_string("host", "127.0.0.1")
    http.set_int("port", 10809)

    proxy = GnomeProxy(backend)
    proxy.set_mode("manual")
    changes: list[None] = []
    proxy.connect_changed(lambda: changes.append(None))
    assert proxy.values()["mode"] == "manual"
    assert proxy.values()["http.host"] == "127.0.0.1"
    assert proxy.values()["http.port"] == 10809

    proxy.set_mode("none")
    assert proxy.values()["mode"] == "none"
    assert changes


class FakeActive:
    def __init__(
        self, connection: NM.Connection | None, state: NM.ActiveConnectionState, *, vpn: bool
    ) -> None:
        self._connection = connection
        self._state = state
        self._vpn = vpn

    def get_vpn(self) -> bool:
        return self._vpn

    def get_connection(self) -> NM.Connection | None:
        return self._connection

    def get_state(self) -> NM.ActiveConnectionState:
        return self._state


class FakeClient:
    def __init__(self, *active: FakeActive) -> None:
        self._active = list(active)

    def get_active_connections(self) -> list[Any]:
        return self._active


def test_vless_active(reality_server: VlessServer) -> None:
    vless = build_connection(reality_server, owner=None)
    other = NM.SimpleConnection.new()
    other.add_setting(NM.SettingVpn(service_type="org.freedesktop.NetworkManager.openvpn"))
    activated = NM.ActiveConnectionState.ACTIVATED

    assert not vless_active(FakeClient())
    assert not vless_active(FakeClient(FakeActive(other, activated, vpn=True)))
    assert not vless_active(
        FakeClient(FakeActive(vless, NM.ActiveConnectionState.ACTIVATING, vpn=True))
    )
    assert not vless_active(FakeClient(FakeActive(None, activated, vpn=True)))
    assert vless_active(
        FakeClient(FakeActive(other, activated, vpn=True), FakeActive(vless, activated, vpn=True))
    )
