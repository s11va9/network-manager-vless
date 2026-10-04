# SPDX-License-Identifier: GPL-2.0-or-later
"""The VPN service started by NetworkManager for each VLESS connection.

Lifecycle of one connection::

    Connect(settings)
      -> validate settings              (sync; errors are returned to NetworkManager)
      -> resolve the server address     (async)
      -> create the TUN device          (root)
      -> spawn xray as user "nm-vless"  (config on stdin, TUN via XRAY_TUN_FD)
      -> wait for "Xray ... started"
      -> report Config / Ip4Config / Ip6Config
    xray exits or Disconnect() -> stop xray, close the TUN device

The service runs as root because NetworkManager starts it; the only privileged
operation it performs is creating the TUN device.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import logging.handlers
import os
import pwd
import shutil
import signal
import sys
import time
from collections import deque
from collections.abc import Callable
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Final

import gi

gi.require_version("NM", "1.0")
from gi.repository import NM, Gio, GLib

from nm_vless import SERVICE_TYPE, __version__, diagnostics, settings, tun, vpnconfig, xray
from nm_vless.link import VlessServer, is_ip_address

LOG: Final = logging.getLogger("nm-vless")

#: Unprivileged account that runs xray, created from data/nm-vless.sysusers.conf.
XRAY_USER: Final = "nm-vless"
TUN_FD_IN_CHILD: Final = 3
READY_TIMEOUT_S: Final = 20
STOP_TIMEOUT_S: Final = 3
#: xray is restarted after unexpected exits, up to MAX_RESTARTS times per RESTART_WINDOW_S.
MAX_RESTARTS: Final = 3
RESTART_WINDOW_S: Final = 300
RESTART_DELAY_S: Final = 2
_OUTPUT_TAIL: Final = 10
_READ_CHUNK: Final = 4096
_MAX_LINE: Final = 8192
_SAFE_PATH: Final = "/usr/sbin:/usr/bin:/sbin:/bin"


def _error(code: NM.VpnPluginError, message: str) -> GLib.Error:
    return GLib.Error.new_literal(NM.VpnPluginError.quark(), message, int(code))


def _privilege_drop_prefix() -> list[str]:
    """Return the ``setpriv`` command prefix that runs xray without privileges."""
    if os.geteuid() != 0:
        return []  # development mode: already unprivileged
    try:
        pwd.getpwnam(XRAY_USER)
    except KeyError:
        LOG.warning("user %r does not exist; running xray as root (see README)", XRAY_USER)
        return []
    setpriv = shutil.which("setpriv", path=_SAFE_PATH)
    if setpriv is None:
        LOG.warning("setpriv not found; running xray as root")
        return []
    # Only CAP_SETUID and CAP_SETGID are needed: NetworkManager.service limits its
    # CapabilityBoundingSet, so options that need CAP_SETPCAP (such as --bounding-set)
    # fail there. Switching from root to another uid clears all capabilities, and
    # --no-new-privs prevents regaining any through exec.
    return [
        setpriv,
        f"--reuid={XRAY_USER}",
        f"--regid={XRAY_USER}",
        "--clear-groups",
        "--no-new-privs",
        "--inh-caps=-all",
        "--",
    ]


class XrayProcess:
    """An xray child process with its output forwarded to the log."""

    def __init__(
        self,
        argv: list[str],
        *,
        tun_fd: int,
        config: bytes,
        on_ready: Callable[[], None],
        on_exit: Callable[[str], None],
    ) -> None:
        self._argv = argv
        self._tun_fd = tun_fd
        self._config = config
        self._on_ready = on_ready
        self._on_exit = on_exit
        self._proc: Gio.Subprocess | None = None
        self._stdout: Gio.InputStream | None = None
        self._pending = b""
        self._ready = False
        self._exited = False
        self._exit_status: str | None = None
        self._output_done = False
        self._exit_reported = False
        self._kill_source = 0
        self.tail: deque[str] = deque(maxlen=_OUTPUT_TAIL)

    def start(self) -> None:
        flags = (
            Gio.SubprocessFlags.STDIN_PIPE
            | Gio.SubprocessFlags.STDOUT_PIPE
            | Gio.SubprocessFlags.STDERR_MERGE
        )
        launcher = Gio.SubprocessLauncher.new(flags)
        launcher.set_environ([f"XRAY_TUN_FD={TUN_FD_IN_CHILD}", f"PATH={_SAFE_PATH}"])
        launcher.set_cwd("/")
        # The launcher owns and closes the duplicate; the service keeps tun_fd.
        launcher.take_fd(os.dup(self._tun_fd), TUN_FD_IN_CHILD)
        self._proc = launcher.spawnv(self._argv)
        # Watch the process before feeding it, so an early exit is always reported
        # through on_exit together with xray's own error message.
        self._proc.wait_async(None, self._on_wait)
        self._stdout = self._proc.get_stdout_pipe()
        self._read_chunk()

        # The configuration contains the user id, so it is never written to disk.
        stdin = self._proc.get_stdin_pipe()
        try:
            stdin.write_all(self._config, None)
            stdin.close(None)
        except GLib.Error as exc:
            LOG.debug("xray closed its input early: %s", exc.message)

    def stop(self) -> None:
        if self._proc is None or self._exited or self._kill_source:
            return
        self._proc.send_signal(signal.SIGTERM)
        self._kill_source = GLib.timeout_add_seconds(STOP_TIMEOUT_S, self._force_exit)

    def _force_exit(self) -> bool:
        self._kill_source = 0
        if self._proc is not None and not self._exited:
            LOG.warning("xray did not exit after SIGTERM, killing it")
            self._proc.force_exit()
        return False  # GLib.SOURCE_REMOVE

    def _read_chunk(self) -> None:
        if self._stdout is not None:
            self._stdout.read_bytes_async(_READ_CHUNK, GLib.PRIORITY_DEFAULT, None, self._on_chunk)

    def _on_chunk(self, stream: Gio.InputStream, result: Gio.AsyncResult) -> None:
        # read_bytes returns an empty buffer only at EOF; DataInputStream.read_line
        # cannot distinguish EOF from an empty line in PyGObject.
        try:
            chunk = stream.read_bytes_finish(result).get_data() or b""
        except GLib.Error as exc:
            LOG.debug("stopped reading xray output: %s", exc.message)
            chunk = b""
        if not chunk:
            if self._pending:
                self._handle_line(self._pending)
                self._pending = b""
            self._output_done = True
            self._report_exit()
            return
        *lines, self._pending = (self._pending + chunk).split(b"\n")
        if len(self._pending) > _MAX_LINE:
            lines.append(self._pending)
            self._pending = b""
        for raw in lines:
            self._handle_line(raw)
        self._read_chunk()

    def _handle_line(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").rstrip()
        if not line:
            return
        LOG.log(_xray_line_level(line), "xray: %s", line)
        self.tail.append(line)
        if not self._ready and xray.READY_RE.search(line):
            self._ready = True
            self._on_ready()

    def _on_wait(self, proc: Gio.Subprocess, result: Gio.AsyncResult) -> None:
        try:
            proc.wait_finish(result)
        except GLib.Error as exc:
            LOG.warning("waiting for xray failed: %s", exc.message)
        self._exited = True
        if self._kill_source:
            GLib.source_remove(self._kill_source)
            self._kill_source = 0
        if proc.get_if_signaled():
            self._exit_status = f"killed by signal {proc.get_term_sig()}"
        else:
            self._exit_status = f"exited with status {proc.get_exit_status()}"
        self._report_exit()

    def _report_exit(self) -> None:
        # Report only after the output is drained, so tail holds xray's last message.
        if self._exit_status is None or not self._output_done or self._exit_reported:
            return
        self._exit_reported = True
        self._on_exit(self._exit_status)


class Session:
    """One active connection: resolver, TUN device and xray process."""

    def __init__(
        self,
        plugin: VlessPlugin,
        server: VlessServer,
        options: settings.TunnelOptions,
        xray_path: Path,
        *,
        default4: bool = True,
        default6: bool = True,
    ) -> None:
        """*default4*/*default6*: route all IPv4/IPv6 traffic through the tunnel."""
        self._plugin = plugin
        self._server = server
        self._options = options
        self._xray_path = xray_path
        self._default4 = default4
        self._default6 = default6
        self._cancellable = Gio.Cancellable()
        self._gateway: IPv4Address | IPv6Address | None = None
        self._tun_fd = -1
        self._tun_name = ""
        self._process: XrayProcess | None = None
        self._argv: list[str] = []
        self._config = b""
        self._timeout_source = 0
        self._restart_source = 0
        self._restarts: deque[float] = deque()
        self._configured = False
        self._stopping = False

    def start(self) -> None:
        if is_ip_address(self._server.address):
            self._launch(ipaddress.ip_address(self._server.address))
            return
        LOG.info("resolving %s", self._server.address)
        Gio.Resolver.get_default().lookup_by_name_async(
            self._server.address, self._cancellable, self._on_resolved
        )

    def stop(self) -> None:
        """Release all resources. Safe to call more than once."""
        self._stopping = True
        self._cancellable.cancel()
        for source in (self._timeout_source, self._restart_source):
            if source:
                GLib.source_remove(source)
        self._timeout_source = self._restart_source = 0
        if self._process is not None:
            self._process.stop()
        if self._tun_fd >= 0:
            os.close(self._tun_fd)
            self._tun_fd = -1

    def _on_resolved(self, resolver: Gio.Resolver, result: Gio.AsyncResult) -> None:
        try:
            addresses = resolver.lookup_by_name_finish(result)
        except GLib.Error as exc:
            if not self._stopping:
                self._fail(f"cannot resolve {self._server.address}: {exc.message}")
            return
        parsed = [ipaddress.ip_address(a.to_string()) for a in addresses]
        # Prefer IPv4: it works on every network, IPv6 connectivity is optional.
        parsed.sort(key=lambda a: a.version)
        if not parsed:
            self._fail(f"{self._server.address} has no addresses")
            return
        self._launch(parsed[0])

    def _launch(self, gateway: IPv4Address | IPv6Address) -> None:
        if self._stopping:
            return
        self._gateway = gateway
        try:
            self._tun_fd, self._tun_name = tun.create_tun()
        except OSError as exc:
            self._fail(f"cannot create TUN device: {exc.strerror}")
            return

        config = xray.build_config(
            self._server,
            connect_address=str(gateway),
            tun_name=self._tun_name,
            mtu=self._options.mtu,
            log_level=_xray_log_level(),
        )
        self._config = json.dumps(config).encode()
        self._argv = [*_privilege_drop_prefix(), str(self._xray_path), "run", "-config", "stdin:"]
        LOG.info(
            "starting xray for %s via %s (%s/%s) on %s",
            self._server.endpoint,
            gateway,
            self._server.network,
            self._server.security,
            self._tun_name,
        )
        self._spawn()

    def _spawn(self) -> None:
        """Start xray on the existing TUN device and wait for it to be ready."""
        self._process = XrayProcess(
            self._argv,
            tun_fd=self._tun_fd,
            config=self._config,
            on_ready=self._on_ready,
            on_exit=self._on_exit,
        )
        try:
            self._process.start()
        except GLib.Error as exc:
            self._fail(f"cannot start xray: {exc.message}")
            return
        self._timeout_source = GLib.timeout_add_seconds(READY_TIMEOUT_S, self._on_timeout)

    def _on_ready(self) -> None:
        if self._stopping or self._gateway is None:
            return
        if self._timeout_source:
            GLib.source_remove(self._timeout_source)
            self._timeout_source = 0
        if self._configured:
            # NetworkManager keeps the device configured while xray restarts.
            LOG.info("xray is running again")
            return
        self._configured = True
        LOG.info("xray is running, configuring %s", self._tun_name)
        plugin = self._plugin
        plugin.set_config(
            vpnconfig.generic_config(
                tundev=self._tun_name,
                mtu=self._options.mtu,
                gateway=self._gateway,
                has_ip6=self._options.ipv6,
            )
        )
        plugin.set_ip4_config(vpnconfig.ip4_config(self._options, split_default=self._default4))
        if self._options.ipv6:
            plugin.set_ip6_config(vpnconfig.ip6_config(self._options, split_default=self._default6))

    def _on_timeout(self) -> bool:
        self._timeout_source = 0
        self._fail(f"xray did not start within {READY_TIMEOUT_S} seconds")
        return False  # GLib.SOURCE_REMOVE

    def _on_exit(self, status: str) -> None:
        if self._stopping:
            LOG.info("xray %s", status)
            return
        details = ""
        if self._process is not None and self._process.tail:
            details = ": " + self._process.tail[-1]
        if self._configured and self._may_restart():
            LOG.warning(
                "xray %s%s; restarting in %d s (%d/%d)",
                status,
                details,
                RESTART_DELAY_S,
                len(self._restarts),
                MAX_RESTARTS,
            )
            self._restart_source = GLib.timeout_add_seconds(RESTART_DELAY_S, self._restart)
            return
        self._fail(f"xray {status}{details}")

    def _may_restart(self) -> bool:
        """Record a restart unless too many happened recently (a crash loop)."""
        now = time.monotonic()
        while self._restarts and now - self._restarts[0] > RESTART_WINDOW_S:
            self._restarts.popleft()
        if len(self._restarts) >= MAX_RESTARTS:
            return False
        self._restarts.append(now)
        return True

    def _restart(self) -> bool:
        self._restart_source = 0
        if not self._stopping:
            self._spawn()
        return False  # GLib.SOURCE_REMOVE

    def _fail(self, message: str) -> None:
        if self._stopping:
            return
        LOG.error("%s", message)
        self.stop()
        self._plugin.report_failure()


class VlessPlugin(NM.VpnServicePlugin):
    """NetworkManager VPN service plugin for VLESS."""

    # need_secrets is deliberately not implemented. libnm's introspection data marks
    # its "setting_name" out-parameter as an input string, so a Python override makes
    # PyGObject read an uninitialised pointer and corrupts the process. Without an
    # override libnm answers "no secrets needed"; NetworkManager has already loaded
    # the system-owned "uuid" secret by then, and do_connect reports a missing one.

    def __init__(self, bus_name: str) -> None:
        super().__init__(service_name=bus_name, watch_peer=True)
        self._session: Session | None = None

    def do_connect(self, connection: NM.Connection) -> bool:
        if self._session is not None:
            raise _error(NM.VpnPluginError.WRONGSTATE, "a connection is already active")

        s_vpn = connection.get_setting_vpn()
        if s_vpn is None or s_vpn.get_service_type() != SERVICE_TYPE:
            raise _error(NM.VpnPluginError.INVALIDCONNECTION, "not a VLESS connection")
        data = {key: s_vpn.get_data_item(key) for key in s_vpn.get_data_keys()}
        secrets = {key: s_vpn.get_secret(key) for key in s_vpn.get_secret_keys()}
        for key in settings.unknown_keys(data):
            LOG.warning("ignoring unknown setting %r", key)

        try:
            server = settings.server_from_items(connection.get_id() or "VLESS", data, secrets)
            options = settings.tunnel_from_items(data)
        except settings.SettingsError as exc:
            raise _error(NM.VpnPluginError.BADARGUMENTS, str(exc)) from exc
        try:
            xray_path = xray.find_xray()
            xray.ensure_supported(xray_path, prefix=_privilege_drop_prefix())
        except xray.XrayError as exc:
            raise _error(NM.VpnPluginError.LAUNCHFAILED, str(exc)) from exc

        LOG.info("connecting %r (%s)", connection.get_id(), server.endpoint)
        _warn_about_competing_tunnels()
        self._session = Session(
            self,
            server,
            options,
            xray_path,
            default4=_takes_default_route(connection.get_setting_ip4_config()),
            default6=_takes_default_route(connection.get_setting_ip6_config()),
        )
        self._session.start()
        return True

    def do_disconnect(self) -> bool:
        if self._session is not None:
            LOG.info("disconnecting")
            self._session.stop()
            self._session = None
        return True

    def report_failure(self) -> None:
        """Tell NetworkManager that the active connection failed and stop it."""
        self.failure(NM.VpnPluginFailure.CONNECT_FAILED)
        try:
            self.disconnect()
        except GLib.Error as exc:
            LOG.debug("disconnect after failure: %s", exc.message)
        self._session = None


def _takes_default_route(setting: NM.SettingIPConfig | None) -> bool:
    """False if the profile keeps the default route elsewhere ("Use this connection
    only for resources on its network")."""
    return setting is None or not setting.get_never_default()


def _warn_about_competing_tunnels() -> None:
    """Log other tunnels whose routes would take traffic away from this VPN."""
    routes = diagnostics.default_routes()
    for route in diagnostics.competing_tunnels(routes, diagnostics.own_devices(routes)):
        LOG.warning(
            "tunnel %s (another VPN client?) has the route %s with metric %d; "
            "while it is up, traffic will not use this VPN",
            route.dev,
            route.destination,
            route.metric,
        )


def _xray_line_level(line: str) -> int:
    """Logging level for a line of xray output ("... [Warning] message")."""
    if "[Error]" in line:
        return logging.ERROR
    if "[Warning]" in line:
        return logging.WARNING
    return logging.INFO


def _xray_log_level() -> str:
    """xray's own log level: per-connection messages only when debugging.

    At "info", xray logs every connection it fails to forward (with its
    destination), which is what troubleshooting needs but too private by default.
    """
    return "info" if LOG.isEnabledFor(logging.DEBUG) else "warning"


def _log_handler(debug: bool) -> logging.Handler:
    """Log to syslog (the journal) when started by NetworkManager.

    NetworkManager discards the standard output and error of VPN services and
    asks them to use syslog instead by setting NM_VPN_LOG_SYSLOG=1.
    """
    if not debug and os.environ.get("NM_VPN_LOG_SYSLOG") == "1":
        try:
            syslog = logging.handlers.SysLogHandler(
                address="/dev/log", facility=logging.handlers.SysLogHandler.LOG_DAEMON
            )
        except OSError:
            pass
        else:
            syslog.ident = f"nm-vless-service[{os.getpid()}]: "
            syslog.setFormatter(logging.Formatter("%(message)s"))
            return syslog
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("nm-vless[%(process)d]: %(levelname)s: %(message)s"))
    return stream


def _log_level_from_env() -> int:
    """Map NetworkManager's ``NM_VPN_LOG_LEVEL`` (syslog priority) to logging levels."""
    try:
        level = int(os.environ.get("NM_VPN_LOG_LEVEL", "6"))
    except ValueError:
        level = 6
    if level >= 7:  # noqa: PLR2004 - LOG_DEBUG
        return logging.DEBUG
    if level >= 5:  # noqa: PLR2004 - LOG_NOTICE
        return logging.INFO
    return logging.WARNING


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NetworkManager VPN service for VLESS")
    parser.add_argument("--bus-name", default=SERVICE_TYPE, help="D-Bus name to own")
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else _log_level_from_env(),
        handlers=[_log_handler(args.debug)],
    )

    plugin = VlessPlugin(args.bus_name)
    try:
        plugin.init(None)
    except GLib.Error as exc:
        LOG.error("cannot register %s on the system bus: %s", args.bus_name, exc.message)
        return 1

    loop = GLib.MainLoop()
    plugin.connect("quit", lambda *_: loop.quit())

    def on_signal() -> bool:
        plugin.do_disconnect()
        loop.quit()
        return False  # GLib.SOURCE_REMOVE

    for signum in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signum, on_signal)

    LOG.debug("nm-vless-service %s ready as %s", __version__, args.bus_name)
    loop.run()
    return 0
