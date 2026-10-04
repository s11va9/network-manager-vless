# SPDX-License-Identifier: GPL-2.0-or-later
"""Drive nm-vless-service over D-Bus the way NetworkManager does and print the results as JSON.

Run by test_service.py inside ``unshare --user --map-root-user --net dbus-run-session``:
the private session bus stands in for the system bus, and the private network namespace
allows creating TUN devices. Only the xray ownership check is relaxed, because files
owned by the real root appear as "nobody" inside a user namespace.

Like NetworkManager, every activation uses a fresh service process: libnm makes the
service exit once a connection attempt has stopped.

Usage: dbus_session.py XRAY_BINARY
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import gi

gi.require_version("NM", "1.0")
from gi.repository import NM, Gio, GLib

from nm_vless.link import VlessServer, parse_link
from nm_vless.nmclient import build_connection

BUS_NAME = "org.freedesktop.NetworkManager.vless.Connection_test"
OBJECT_PATH = "/org/freedesktop/NetworkManager/VPN/Plugin"
IFACE = "org.freedesktop.NetworkManager.VPN.Plugin"
UUID = "b831381d-6324-4d53-ad4f-8cda48b30811"
LINK = f"vless://{UUID}@203.0.113.10:443?security=tls&sni=example.com&type=xhttp&path=/x#t"

SERVICE_CODE = """
import faulthandler
import sys
from pathlib import Path
from nm_vless import service, xray
faulthandler.enable()
xray.check_binary = lambda path: Path(path)  # see module docstring
service._privilege_drop_prefix = list  # only uid 0 exists in the namespace
sys.exit(service.main(sys.argv[1:]))
"""


def _connection(
    server: VlessServer, *, with_secret: bool = True, data: dict[str, str] | None = None
) -> GLib.Variant:
    connection = build_connection(server, owner=None)
    s_vpn = connection.get_setting_vpn()
    if not with_secret:
        s_vpn.remove_secret("uuid")
    for key, value in (data or {}).items():
        s_vpn.add_data_item(key, value)
    return GLib.Variant.new_tuple(connection.to_dbus(NM.ConnectionSerializationFlags.ALL))


def _tun_exists() -> bool:
    links = subprocess.run(["ip", "-brief", "link"], capture_output=True, text=True, check=False)
    return "vless" in links.stdout


def _iterate(seconds: float, done: list[bool] | None = None) -> None:
    """Run the default main context for *seconds* or until ``done[0]`` becomes true."""
    context = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + int(seconds * 1_000_000)
    while GLib.get_monotonic_time() < deadline and not (done and done[0]):
        if not context.iteration(False):
            GLib.usleep(10_000)


class Service:
    """One nm-vless-service process and a D-Bus proxy to it."""

    def __init__(self, bus: Gio.DBusConnection, xray_binary: str) -> None:
        env = {**os.environ, "NM_VLESS_XRAY": xray_binary}
        self.process = subprocess.Popen(
            [sys.executable, "-c", SERVICE_CODE, "--bus-name", BUS_NAME, "--debug"], env=env
        )
        appeared = [False]

        def on_appeared(*_args: Any) -> None:
            appeared[0] = True

        watch = Gio.bus_watch_name_on_connection(
            bus, BUS_NAME, Gio.BusNameWatcherFlags.NONE, on_appeared, None
        )
        _iterate(10, appeared)
        Gio.bus_unwatch_name(watch)

        self.signals: list[str] = []
        self.states: list[int] = []
        self.configured = [False]
        self.proxy = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None, BUS_NAME, OBJECT_PATH, IFACE, None
        )
        self.proxy.connect("g-signal", self._on_signal)

    def _on_signal(self, _proxy: Any, _sender: Any, name: str, params: GLib.Variant) -> None:
        if name == "StateChanged":
            self.states.append(params.unpack()[0])
            return
        self.signals.append(name)
        if name in ("Ip6Config", "Failure"):
            self.configured[0] = True

    def call(self, method: str, args: GLib.Variant | None = None) -> Any:
        reply = self.proxy.call_sync(method, args, Gio.DBusCallFlags.NONE, 30000, None)
        return reply.unpack() if reply is not None else None

    def call_error(self, method: str, args: GLib.Variant | None = None) -> str | None:
        try:
            self.call(method, args)
        except GLib.Error as exc:
            return str(Gio.DBusError.get_remote_error(exc))
        return None

    def wait_exit(self) -> int | None:
        try:
            return self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return None


def main() -> int:
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = os.environ["DBUS_SESSION_BUS_ADDRESS"]
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    server = parse_link(LINK)
    result: dict[str, Any] = {}

    # Activation 1: secrets handling and a connection rejected by validation.
    first = Service(bus, sys.argv[1])
    result["need_secrets"] = first.call("NeedSecrets", _connection(server))[0]
    result["need_secrets_without_uuid"] = first.call(
        "NeedSecrets", _connection(server, with_secret=False)
    )[0]
    result["invalid_error"] = first.call_error(
        "Connect", _connection(server, data={"network": "kcp"})
    )
    result["invalid_exit"] = first.wait_exit()

    # Activation 2: a connection without its secret.
    second = Service(bus, sys.argv[1])
    result["missing_secret_error"] = second.call_error(
        "Connect", _connection(server, with_secret=False)
    )
    second.wait_exit()

    # Activation 3: a valid connection, then Disconnect.
    third = Service(bus, sys.argv[1])
    third.call("Connect", _connection(server))
    _iterate(25, third.configured)
    result["tun_while_connected"] = _tun_exists()
    third.call("Disconnect")
    _iterate(2)
    result["signals"] = third.signals
    result["states"] = third.states
    result["tun_after_disconnect"] = _tun_exists()
    third.process.terminate()
    third.wait_exit()

    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
