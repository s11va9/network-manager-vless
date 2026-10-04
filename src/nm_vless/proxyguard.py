# SPDX-License-Identifier: GPL-2.0-or-later
"""Turn off another client's system proxy while a VLESS connection is up.

Proxy clients such as Happ or v2rayN point the GNOME system proxy to their own local
proxy (``127.0.0.1:…``). Browsers and most desktop applications then send their
traffic to that proxy instead of through the routes, so it bypasses VLESS, or fails
once the other client stops.

``nm-vless proxy-guard`` runs in the desktop session (``nm-vless-proxy-guard.service``).
While a VLESS connection is active and the system proxy points to a loopback address,
it sets ``org.gnome.system.proxy mode`` to ``'none'``; when no VLESS connection is
active any more, it restores the previous mode, unless the user changed it meanwhile.
Only the mode is changed, so the proxy addresses stay as they were. The previous mode
is kept in ``$XDG_STATE_HOME/nm-vless/system-proxy-mode``, so it is restored even if
the guard was not running when the connection went down.
"""

from __future__ import annotations

import logging
import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Final, Protocol

import gi

gi.require_version("NM", "1.0")
gi.require_version("Gio", "2.0")
from gi.repository import NM, Gio, GLib

from nm_vless import SERVICE_TYPE, diagnostics
from nm_vless.buildinfo import translation

__all__ = ["GnomeProxy", "ProxyGuard", "ProxySettings", "run"]

LOG = logging.getLogger("nm-vless.proxy-guard")
_ = translation().gettext

SCHEMA: Final = "org.gnome.system.proxy"
_KINDS: Final = ("http", "https", "socks")
_MODES: Final = frozenset({"none", "manual", "auto"})


class ProxySettings(Protocol):
    def values(self) -> dict[str, object]:
        """``mode``, ``http.host``, ``http.port``, … (see :func:`diagnostics.local_proxy`)."""
        ...

    def set_mode(self, mode: str) -> None: ...


class GnomeProxy:
    """The ``org.gnome.system.proxy`` settings of the current user."""

    def __init__(self, backend: Gio.SettingsBackend | None = None) -> None:
        def new(schema: str) -> Gio.Settings:
            if backend is None:
                return Gio.Settings.new(schema)
            return Gio.Settings.new_with_backend(schema, backend)

        self._settings = new(SCHEMA)
        self._children = {kind: new(f"{SCHEMA}.{kind}") for kind in _KINDS}

    @staticmethod
    def available() -> bool:
        source = Gio.SettingsSchemaSource.get_default()
        return source is not None and source.lookup(SCHEMA, True) is not None

    def values(self) -> dict[str, object]:
        values: dict[str, object] = {"mode": self._settings.get_string("mode")}
        for kind, child in self._children.items():
            values[f"{kind}.host"] = child.get_string("host")
            values[f"{kind}.port"] = child.get_int("port")
        return values

    def set_mode(self, mode: str) -> None:
        self._settings.set_string("mode", mode)
        Gio.Settings.sync()

    def connect_changed(self, callback: Callable[[], None]) -> None:
        for settings in (self._settings, *self._children.values()):
            settings.connect("changed", lambda *_: callback())


def _default_state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "nm-vless" / "system-proxy-mode"


class ProxyGuard:
    """Keeps a local system proxy off while VLESS is up; see the module documentation."""

    def __init__(
        self,
        proxy: ProxySettings,
        state_path: Path | None = None,
        notify: Callable[[str, str], None] | None = None,
    ) -> None:
        self._proxy = proxy
        self._state_path = state_path or _default_state_path()
        self._notify = notify
        self._vless_up = False

    def _saved_mode(self) -> str | None:
        try:
            mode = self._state_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError as exc:
            LOG.warning("cannot read %s: %s", self._state_path, exc)
            return None
        return mode if mode in _MODES else None

    def _save_mode(self, mode: str) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(mode + "\n", encoding="utf-8")

    def _forget_mode(self) -> None:
        self._state_path.unlink(missing_ok=True)

    def set_vless_up(self, up: bool) -> None:
        if up != self._vless_up:
            LOG.info("VLESS is %s", "connected" if up else "disconnected")
        self._vless_up = up
        self.check()

    def check(self) -> None:
        """Apply the rule to the current settings; call it whenever they change."""
        values = self._proxy.values()
        mode = str(values.get("mode"))
        if self._vless_up:
            local = diagnostics.local_proxy(values)
            if local is None:
                return
            if self._saved_mode() is None:
                self._save_mode(mode)
            # Calls check() again through the change signal, which then finds no proxy.
            self._proxy.set_mode("none")
            LOG.info("turned off the system proxy %s while VLESS is connected", local)
            if self._notify:
                self._notify(
                    _("System proxy turned off"),
                    _(
                        "The system proxy {proxy} belongs to another VPN client and would "
                        "bypass VLESS. It is turned back on when VLESS disconnects."
                    ).format(proxy=local),
                )
            return
        saved = self._saved_mode()
        if saved is None:
            return
        # Forget first: setting the mode calls check() again through the change signal.
        self._forget_mode()
        if mode == "none":
            self._proxy.set_mode(saved)
            LOG.info("restored the system proxy (mode %r)", saved)
        else:
            LOG.info("system proxy mode was changed to %r meanwhile; not restoring it", mode)

    def stop(self) -> None:
        """Restore the proxy before exiting: the guard only changes it while it runs."""
        self.set_vless_up(False)


def _is_vless(active: NM.ActiveConnection) -> bool:
    if not active.get_vpn():
        return False
    connection = active.get_connection()
    s_vpn = connection.get_setting_vpn() if connection is not None else None
    return s_vpn is not None and s_vpn.get_service_type() == SERVICE_TYPE


def vless_active(client: NM.Client) -> bool:
    """True if a VLESS connection is fully connected."""
    return any(
        _is_vless(active) and active.get_state() == NM.ActiveConnectionState.ACTIVATED
        for active in client.get_active_connections()
    )


class _Watcher:
    """Calls *changed* whenever the state of any active connection may have changed."""

    def __init__(self, client: NM.Client, changed: Callable[[], None]) -> None:
        self._client = client
        self._changed = changed
        self._watched: set[str] = set()
        client.connect("notify::active-connections", self._on_list)
        client.connect("notify::nm-running", self._on_list)
        self._on_list()

    def _on_list(self, *_: object) -> None:
        current = set()
        for active in self._client.get_active_connections():
            path = active.get_path()
            current.add(path)
            if path not in self._watched:
                active.connect("notify::state", lambda *_: self._changed())
        self._watched = current
        self._changed()


def _desktop_notification(summary: str, body: str) -> None:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as exc:
        LOG.debug("no session bus: %s", exc.message)
        return
    bus.call(
        "org.freedesktop.Notifications",
        "/org/freedesktop/Notifications",
        "org.freedesktop.Notifications",
        "Notify",
        GLib.Variant(
            "(susssasa{sv}i)",
            ("VLESS", 0, "network-vpn-symbolic", summary, body, [], {}, -1),
        ),
        None,
        Gio.DBusCallFlags.NONE,
        -1,
        None,
        None,
    )


def run() -> int:
    """Run the guard until SIGTERM or SIGINT."""
    if not GnomeProxy.available():
        LOG.info("%s is not available (not a GNOME session); nothing to do", SCHEMA)
        return 0
    try:
        client = NM.Client.new(None)
    except GLib.Error as exc:
        LOG.error("cannot connect to NetworkManager: %s", exc.message)
        return 1

    proxy = GnomeProxy()
    guard = ProxyGuard(proxy, notify=_desktop_notification)
    loop = GLib.MainLoop()
    proxy.connect_changed(guard.check)
    _Watcher(client, lambda: guard.set_vless_up(vless_active(client)))

    def quit_loop() -> bool:
        loop.quit()
        return False  # GLib.SOURCE_REMOVE

    for signum in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, quit_loop)
    loop.run()
    guard.stop()
    return 0
