# SPDX-License-Identifier: GPL-2.0-or-later
"""The ``nm-vless`` command: import links and manage subscriptions.

Links and subscription URLs are credentials. Pass ``-`` instead of the value to
read it from standard input (hidden when typed in a terminal), which keeps it
out of the shell history and the process list.
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import logging
import os
import pwd
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import gi

gi.require_version("NM", "1.0")
from gi.repository import NM, GLib

from nm_vless import SERVICE_TYPE, __version__, diagnostics, probe, settings, xray
from nm_vless.link import LinkError, format_link, parse_link
from nm_vless.nmclient import (
    TUNNEL_DNS_PRIORITY,
    NMError,
    NMSession,
    build_connection,
    leaks_dns,
    owned_by_subscription,
    subscription_of,
    vpn_data,
)
from nm_vless.subscription import (
    DEFAULT_USER_AGENT,
    Subscription,
    SubscriptionError,
    SubscriptionStore,
    fetch,
    parse_subscription,
    validate_url,
)
from nm_vless.sync import SyncReport, sync_subscription

_ERRORS = (LinkError, NMError, SubscriptionError, settings.SettingsError, xray.XrayError)


class CliError(RuntimeError):
    """An error reported to the user without a traceback."""


def _current_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def _read_secret(value: str, prompt: str) -> str:
    if value != "-":
        return value
    if sys.stdin.isatty():
        return getpass.getpass(f"{prompt}: ").strip()
    return sys.stdin.readline().strip()


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "?"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":  # noqa: PLR2004
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"  # pragma: no cover


def _table(rows: Sequence[Sequence[str]], header: Sequence[str]) -> str:
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    lines = [
        "  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()
        for row in [header, *rows]
    ]
    return "\n".join(lines)


# --- commands ---------------------------------------------------------------------------------


def cmd_import(args: argparse.Namespace) -> int:
    server = parse_link(_read_secret(args.link, "vless:// link"))
    if args.name:
        server = dataclasses.replace(server, name=args.name)
    tunnel = settings.TunnelOptions(ipv6=not args.no_ipv6)
    owner = None if args.shared else _current_user()
    remote = NMSession().add(build_connection(server, owner=owner, tunnel=tunnel))
    print(f"Created connection {server.name!r} ({remote.get_uuid()})")
    print(f"Connect with: nmcli connection up uuid {remote.get_uuid()}")
    return 0


def link_to_keyfile(link: str) -> str:
    """Return the connection settings for *link* in GKeyFile format.

    Used by the GNOME editor plugin, which keeps all link parsing in one place.
    Groups: ``[connection]`` (``id``), ``[vpn]`` (vpn.data) and ``[vpn-secrets]``.
    """
    server = parse_link(link)
    data, secrets = settings.server_to_items(server)
    keyfile = GLib.KeyFile()
    keyfile.set_string("connection", "id", server.name)
    for key, value in data.items():
        keyfile.set_string("vpn", key, value)
    for key, value in secrets.items():
        keyfile.set_string("vpn-secrets", key, value)
    text, _length = keyfile.to_data()
    return str(text)


def cmd_parse_link(_args: argparse.Namespace) -> int:
    sys.stdout.write(link_to_keyfile(sys.stdin.read().strip()))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    nm = NMSession()
    remote = nm.find(args.connection)
    server = settings.server_from_items(remote.get_id(), vpn_data(remote), nm.secrets(remote))
    print(format_link(server))
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """Fetch URLs through a server without connecting the VPN."""
    for url in args.urls:
        if urlsplit(url).scheme != "https":
            raise CliError(f"{url}: only https:// URLs can be tested")
    nm = NMSession()
    remote = nm.find(args.connection)
    server = settings.server_from_items(remote.get_id(), vpn_data(remote), nm.secrets(remote))
    print(f"Testing {server.name!r} ({server.network}/{server.security}), VPN not connected...")
    report = probe.run_probe(server, [*probe.DEFAULT_URLS, *args.urls], timeout=args.timeout)
    for result in report.results:
        mark = "ok" if result.ok else "FAIL"
        print(f"[{mark}] {result.url}: {result.detail} ({result.seconds:.1f} s)")
    if report.exit_ip:
        print(f"Traffic leaves from {report.exit_ip} ({report.exit_country or 'unknown country'})")
    if report.xray_messages:
        print("xray messages:")
        for line in report.xray_messages[-20:]:
            print(f"  {line}")
    failed = [r for r in report.results if not r.ok]
    if failed and len(failed) < len(report.results):
        print(
            "Some sites work and others do not: the server (or its hosting provider) does not "
            "forward traffic to them. Try another server; browsers show this as "
            "'connection closed' (ERR_CONNECTION_CLOSED)."
        )
    elif failed:
        print("Nothing opens through this server: check its settings or try another server.")
    return 0 if not failed else 1


def cmd_list(_args: argparse.Namespace) -> int:
    nm = NMSession()
    active = nm.active_uuids()
    subs = {s.id: s.name for s in SubscriptionStore().load()}
    rows = []
    for remote in sorted(nm.vless_connections(), key=lambda c: c.get_id().lower()):
        data = vpn_data(remote)
        sub_id = subscription_of(remote)
        rows.append(
            (
                "*" if remote.get_uuid() in active else "",
                remote.get_id(),
                f"{data.get('address', '?')}:{data.get('port', '?')}",
                f"{data.get('network', 'tcp')}/{data.get('security', 'none')}",
                subs.get(sub_id, sub_id or "") if sub_id else "",
                remote.get_uuid(),
            )
        )
    if not rows:
        print("No VLESS connections. Add one with: nm-vless import -")
        return 0
    print(_table(rows, ("", "NAME", "SERVER", "TRANSPORT", "SUBSCRIPTION", "UUID")))
    return 0


def _update_subscription(nm: NMSession, sub: Subscription, *, quiet: bool) -> SyncReport | None:
    """Download *sub* and synchronise its connections; messages go to stderr/stdout."""
    sub.mark_attempted()
    try:
        result = fetch(sub.url, user_agent=sub.user_agent or DEFAULT_USER_AGENT)
        parsed = parse_subscription(result.body, result.headers)
        for error in parsed.errors:
            print(f"{sub.name}: skipped {error}", file=sys.stderr)
        if parsed.unsupported and not quiet:
            kinds = ", ".join(f"{k}: {n}" for k, n in sorted(parsed.unsupported.items()))
            print(f"{sub.name}: skipped unsupported links: {kinds}", file=sys.stderr)
        report = sync_subscription(nm, sub.id, parsed.servers, owner=_current_user())
    except (*_ERRORS, ValueError) as exc:
        sub.last_error = str(exc)
        print(f"{sub.name}: {exc}", file=sys.stderr)
        return None
    sub.mark_updated(parsed)
    if not quiet:
        print(f"{sub.name}: {report.summary()}")
    return report


TIMER_UNIT = "nm-vless-subscriptions.timer"
PROXY_GUARD_UNIT = "nm-vless-proxy-guard.service"


def _ensure_update_timer() -> str | None:
    """Start the user timer that updates subscriptions; return a problem, if any.

    Packages enable the timer globally, but a running session only picks it up at
    the next login, so it is started here as soon as the first subscription exists.
    """
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return "systemctl not found; run 'nm-vless subscription update' periodically"
    completed = subprocess.run(
        [systemctl, "--user", "enable", "--now", TIMER_UNIT],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        return f"cannot start {TIMER_UNIT}: {completed.stderr.strip() or completed.returncode}"
    return None


def report_to_keyfile(sub: Subscription, report: SyncReport) -> str:
    """Summary of an added subscription in GKeyFile format (read by the editor)."""
    keyfile = GLib.KeyFile()
    keyfile.set_string("subscription", "id", sub.id)
    keyfile.set_string("subscription", "name", sub.name)
    keyfile.set_integer("subscription", "update-interval-hours", sub.update_interval_hours)
    for field in ("added", "updated", "unchanged", "removed", "kept_active"):
        keyfile.set_integer("subscription", field.replace("_", "-"), len(getattr(report, field)))
    text, _length = keyfile.to_data()
    return str(text)


def cmd_sub_add(args: argparse.Namespace) -> int:
    url = validate_url(_read_secret(args.url, "Subscription URL"))
    store = SubscriptionStore()
    subs = store.load()
    if any(s.url == url for s in subs):
        raise CliError("this subscription is already added")

    user_agent = args.user_agent or ""
    result = fetch(url, user_agent=user_agent or DEFAULT_USER_AGENT)
    parsed = parse_subscription(result.body, result.headers)
    name = args.name or parsed.title or f"subscription-{len(subs) + 1}"
    if any(s.name == name for s in subs):
        raise CliError(f"a subscription named {name!r} already exists, use --name")

    sub = Subscription(id=Subscription.new_id(), url=url, name=name, user_agent=user_agent)
    report = _update_subscription(NMSession(), sub, quiet=args.keyfile)
    if report is None:
        return 1
    subs.append(sub)
    store.save(subs)
    timer_problem = _ensure_update_timer()
    if args.keyfile:
        sys.stdout.write(report_to_keyfile(sub, report))
    else:
        print(
            f"Added subscription {name!r} (id {sub.id}), updated every "
            f"{sub.update_interval_hours} h"
        )
    if timer_problem:
        print(f"nm-vless: warning: {timer_problem}", file=sys.stderr)
    return 0


def cmd_sub_update(args: argparse.Namespace) -> int:
    store = SubscriptionStore()
    subs = store.load()
    selected = [store.get(subs, ident) for ident in args.ids] if args.ids else subs
    if args.due:
        selected = [sub for sub in selected if sub.is_due()]
    if not selected:
        if not args.quiet and not args.due:
            print("No subscriptions. Add one with: nm-vless subscription add -")
        return 0
    nm = NMSession()
    results = [_update_subscription(nm, sub, quiet=args.quiet) for sub in selected]
    store.save(subs)
    return 0 if all(r is not None for r in results) else 1


def cmd_sub_list(args: argparse.Namespace) -> int:
    subs = SubscriptionStore().load()
    if not subs:
        print("No subscriptions. Add one with: nm-vless subscription add -")
        return 0
    nm = NMSession()
    connections = nm.vless_connections()
    rows = []
    for sub in subs:
        traffic = sub.traffic
        used = (traffic.get("upload") or 0) + (traffic.get("download") or 0)
        usage = (
            f"{_format_bytes(used)} / {_format_bytes(traffic.get('total'))}"
            if traffic.get("total")
            else ""
        )
        expire = traffic.get("expire")
        rows.append(
            (
                sub.id,
                sub.name,
                str(len(owned_by_subscription(connections, sub.id))),
                sub.last_update or "never",
                usage,
                datetime.fromtimestamp(expire, UTC).date().isoformat() if expire else "",
                "error: " + sub.last_error if sub.last_error else "",
            )
        )
    print(_table(rows, ("ID", "NAME", "SERVERS", "UPDATED", "TRAFFIC", "EXPIRES", "STATUS")))
    if args.show_urls:
        for sub in subs:
            print(f"{sub.id}: {sub.url}")
    return 0


def cmd_sub_remove(args: argparse.Namespace) -> int:
    store = SubscriptionStore()
    subs = store.load()
    sub = store.get(subs, args.id)
    if not args.keep_connections:
        nm = NMSession()
        for remote in owned_by_subscription(nm.vless_connections(), sub.id):
            nm.delete(remote)
            print(f"Deleted connection {remote.get_id()!r}")
    subs.remove(sub)
    store.save(subs)
    print(f"Removed subscription {sub.name!r}")
    return 0


def cmd_proxy_guard(_args: argparse.Namespace) -> int:
    from nm_vless import proxyguard  # noqa: PLC0415 - desktop only

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return proxyguard.run()


def cmd_check(_args: argparse.Namespace) -> int:
    """Verify that the plugin and its dependencies are installed correctly."""
    ok = True

    def report(passed: bool, message: str) -> None:
        nonlocal ok
        ok &= passed
        print(f"[{'ok' if passed else 'FAIL'}] {message}")

    def warn(message: str) -> None:
        print(f"[warn] {message}")

    plugins = [p for p in NM.VpnPluginInfo.list_load() if p.get_service() == SERVICE_TYPE]
    report(
        bool(plugins),
        f"plugin registered: {plugins[0].get_filename()}"
        if plugins
        else "plugin not registered with NetworkManager (is nm-vless-service.name installed?)",
    )
    if plugins:
        program = plugins[0].get_program()
        report(
            bool(program) and os.access(program, os.X_OK),
            f"service program: {program or 'missing'}",
        )
    try:
        path = xray.find_xray()
        report(True, f"xray: {path} ({xray.ensure_supported(path)})")
    except xray.XrayError as exc:
        report(False, f"xray: {exc}")
    try:
        pwd.getpwnam("nm-vless")
        report(True, "user nm-vless exists (xray runs without privileges)")
    except KeyError:
        report(False, "user nm-vless is missing: run 'sudo systemd-sysusers'")
    report(Path("/dev/net/tun").exists(), "TUN support (/dev/net/tun)")
    try:
        report(True, f"NetworkManager {NMSession().client.get_version()} is running")
    except NMError as exc:
        report(False, str(exc))
        return 1

    for problem in bypass_problems() + dns_problems(NMSession()):
        warn(problem)
    return 0 if ok else 1


def dns_problems(nm: NMSession) -> list[str]:
    """Profiles created before 0.3.1 (or elsewhere) that may leak DNS queries."""
    return [
        f"{remote.get_id()!r} may send DNS queries outside the tunnel; fix it with: "
        f"nmcli connection modify uuid {remote.get_uuid()} "
        f"ipv4.dns-priority {TUNNEL_DNS_PRIORITY} ipv6.dns-priority {TUNNEL_DNS_PRIORITY}"
        for remote in nm.vless_connections()
        if leaks_dns(remote)
    ]


def _proxy_guard_running() -> bool:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return False
    try:
        return (
            subprocess.run(
                [systemctl, "--user", "--quiet", "is-active", PROXY_GUARD_UNIT],
                check=False,
                timeout=5,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def bypass_problems() -> list[str]:
    """Explain configurations that make traffic bypass the VLESS tunnel."""
    routes = diagnostics.default_routes()
    problems = [
        f"tunnel {route.dev} (another VPN client?) has the route {route.destination} "
        f"with metric {route.metric}; while it is up, traffic does not use VLESS. "
        "Disconnect the other client."
        for route in diagnostics.competing_tunnels(routes, diagnostics.own_devices(routes))
    ]
    proxy = diagnostics.gnome_proxy_settings()
    local = diagnostics.local_proxy(proxy) if proxy else None
    if local and not _proxy_guard_running():
        problems.append(
            f"the system proxy points to {local} (another VPN client's local proxy); "
            "browsers and other applications that use it bypass VLESS. "
            f"{PROXY_GUARD_UNIT} turns it off while VLESS is connected; enable it "
            f"with: systemctl --user enable --now {PROXY_GUARD_UNIT}"
        )
    variables = diagnostics.env_local_proxies(os.environ)
    if variables:
        problems.append(
            f"{', '.join(variables)} in this session point to a local proxy; programs "
            "started with these variables bypass VLESS and fail once that proxy stops. "
            "Remove them and restart the programs (or log out and back in)."
        )
    return problems


# --- argument parsing ------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nm-vless",
        description="Manage VLESS connections for NetworkManager.",
        epilog="Pass '-' instead of a link or URL to enter it without exposing it in "
        "the shell history.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(
        name: str,
        func: Callable[[argparse.Namespace], int],
        help_text: str,
        aliases: Sequence[str] = (),
    ) -> argparse.ArgumentParser:
        sub = commands.add_parser(
            name, help=help_text, description=help_text, aliases=list(aliases)
        )
        sub.set_defaults(func=func)
        return sub

    p = add("import", cmd_import, "create a connection from a vless:// link")
    p.add_argument("link", help="vless:// link, or '-' to read it from stdin")
    p.add_argument("--name", help="connection name (default: name from the link)")
    p.add_argument(
        "--shared", action="store_true", help="make the connection available to all users"
    )
    p.add_argument("--no-ipv6", action="store_true", help="do not route IPv6 through the tunnel")

    add(
        "parse-link",
        cmd_parse_link,
        "read a vless:// link from stdin and print its settings (GKeyFile, for the editor)",
    )

    p = add("export", cmd_export, "print the vless:// link of a connection")
    p.add_argument("connection", help="connection name or UUID")

    add("list", cmd_list, "list VLESS connections", aliases=["ls"])

    p = add("test", cmd_test, "check that sites open through a server, without connecting")
    p.add_argument("connection", help="connection name or UUID")
    p.add_argument("urls", nargs="*", metavar="URL", help="additional https:// URLs to fetch")
    p.add_argument(
        "--timeout", type=float, default=15.0, help="seconds per URL (default: %(default)s)"
    )
    add("check", cmd_check, "check the installation")
    add(
        "proxy-guard",
        cmd_proxy_guard,
        "turn off another client's local system proxy while VLESS is connected "
        "(run by nm-vless-proxy-guard.service)",
    )

    p = add("subscription", lambda _a: 2, "manage subscriptions", aliases=["sub"])
    subs = p.add_subparsers(dest="sub_command", required=True, metavar="ACTION")

    s = subs.add_parser("add", help="add a subscription and import its servers")
    s.add_argument("url", help="https:// URL, or '-' to read it from stdin")
    s.add_argument("--name", help="subscription name (default: title sent by the provider)")
    s.add_argument("--user-agent", help=f"HTTP User-Agent (default: {DEFAULT_USER_AGENT})")
    s.add_argument("--keyfile", action="store_true", help=argparse.SUPPRESS)  # for the editor
    s.set_defaults(func=cmd_sub_add)

    s = subs.add_parser("update", help="download subscriptions and update connections")
    s.add_argument("ids", nargs="*", metavar="ID", help="subscriptions to update (default: all)")
    s.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    s.add_argument(
        "--due",
        action="store_true",
        help="only subscriptions whose update interval has passed (used by the timer)",
    )
    s.set_defaults(func=cmd_sub_update)

    s = subs.add_parser("list", aliases=["ls"], help="list subscriptions")
    s.add_argument("--show-urls", action="store_true", help="also print the URLs (secret!)")
    s.set_defaults(func=cmd_sub_list)

    s = subs.add_parser("remove", aliases=["rm"], help="remove a subscription")
    s.add_argument("id", help="subscription id or name")
    s.add_argument(
        "--keep-connections", action="store_true", help="keep the connections created from it"
    )
    s.set_defaults(func=cmd_sub_remove)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except (*_ERRORS, CliError) as exc:
        print(f"nm-vless: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return result
