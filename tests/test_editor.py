# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests of the GNOME Settings editor plugins built by Meson (C, GTK 4).

The plugins are loaded through libnm exactly as GNOME Settings does. Tests that
create GTK widgets need a display; set GDK_BACKEND=broadway and BROADWAY_DISPLAY
to run them headless.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import gi
import pytest

gi.require_version("NM", "1.0")
from gi.repository import NM, GLib

from nm_vless import SERVICE_TYPE, settings
from nm_vless.link import parse_link
from nm_vless.nmclient import build_connection, vpn_data

from .conftest import GRPC_LINK, REALITY_LINK, TEST_UUID, WS_LINK, XHTTP_LINK

ROOT = Path(__file__).resolve().parent.parent
BUILD = Path(os.environ.get("NM_VLESS_BUILD_DIR", ROOT / "build"))
PLUGIN = BUILD / "src" / "editor" / "libnm-vpn-plugin-vless.so"
HEADER = ROOT / "src" / "editor" / "nm-vless-common.h"


def test_c_keys_match_python() -> None:
    defines = dict(re.findall(r'#define NM_VLESS_KEY_(\w+)\s+"([^"]+)"', HEADER.read_text()))
    assert set(defines.values()) == set(settings.KNOWN_DATA_KEYS)
    secret = re.search(r'#define NM_VLESS_SECRET_UUID\s+"([^"]+)"', HEADER.read_text())
    assert secret is not None
    assert secret.group(1) == settings.SECRET_UUID


needs_plugin = pytest.mark.skipif(not PLUGIN.exists(), reason="editor not built (meson compile)")


@pytest.fixture(scope="session")
def plugin(tmp_path_factory: pytest.TempPathFactory) -> Iterator[NM.VpnEditorPlugin]:
    """The plugin, loaded once: a module registers its GTypes only once per process."""
    tmp_path = tmp_path_factory.mktemp("editor")
    # The plugin runs "nm-vless parse-link"; use the source tree instead of an installation.
    cli = tmp_path / "nm-vless"
    cli.write_text(f'#!/bin/sh\nPYTHONPATH={ROOT / "src"} exec {sys.executable} -m nm_vless "$@"\n')
    cli.chmod(0o755)
    # libnm refuses plugins that others can modify, like the group-writable build output.
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir(mode=0o755)
    for library in PLUGIN.parent.glob("*.so"):
        copy = plugin_dir / library.name
        shutil.copyfile(library, copy)
        copy.chmod(0o755)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("NM_VLESS_CLI", str(cli))
        yield NM.VpnEditorPlugin.load_from_file(
            str(plugin_dir / PLUGIN.name), SERVICE_TYPE, os.getuid(), lambda *_args: True, None
        )


@needs_plugin
def test_plugin_properties(plugin: NM.VpnEditorPlugin) -> None:
    assert plugin.props.service == SERVICE_TYPE
    assert plugin.props.name == "VLESS (Xray)"
    assert "REALITY" in plugin.props.description
    capabilities = plugin.get_capabilities()
    assert capabilities & NM.VpnEditorPluginCapability.IMPORT
    assert not capabilities & NM.VpnEditorPluginCapability.EXPORT


@needs_plugin
def test_import_file_with_link(plugin: NM.VpnEditorPlugin, tmp_path: Path) -> None:
    path = tmp_path / "server.txt"
    path.write_text(f"My provider\n\n  {XHTTP_LINK}  \nother text\n")
    connection = plugin.import_(str(path))
    assert connection.get_id() == "XHTTP"
    assert connection.get_setting_vpn().get_service_type() == SERVICE_TYPE
    assert connection.get_setting_vpn().get_secret("uuid") == TEST_UUID
    server = settings.server_from_items(
        connection.get_id(), vpn_data(connection), {"uuid": TEST_UUID}
    )
    assert server == parse_link(XHTTP_LINK)


@needs_plugin
def test_import_rejects_other_files(plugin: NM.VpnEditorPlugin, tmp_path: Path) -> None:
    path = tmp_path / "client.ovpn"
    path.write_text("client\nremote vpn.example.com 1194\n")
    with pytest.raises(GLib.Error, match="does not contain a vless:// link"):
        plugin.import_(str(path))


@needs_plugin
def test_import_reports_invalid_link(plugin: NM.VpnEditorPlugin, tmp_path: Path) -> None:
    path = tmp_path / "bad.txt"
    path.write_text(f"vless://{TEST_UUID}@example.com:443?type=kcp\n")
    with pytest.raises(GLib.Error, match="unsupported transport 'kcp'"):
        plugin.import_(str(path))


# ---- GTK 4 editor ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gtk() -> Any:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    # Imported lazily: loading GTK is only possible where a display may exist.
    from gi.repository import Gdk, Gtk  # noqa: PLC0415

    # PyGObject's init_check() reports success even without a display.
    Gtk.init_check()
    if Gdk.Display.get_default() is None:
        pytest.skip("no display for GTK 4 (set GDK_BACKEND=broadway)")
    return Gtk


def _round_trip(plugin: NM.VpnEditorPlugin, connection: NM.SimpleConnection) -> NM.Connection:
    editor = plugin.get_editor(connection)
    result = NM.SimpleConnection.new_clone(connection)
    assert editor.update_connection(result)
    return result


def _secrets(connection: NM.Connection) -> dict[str, str]:
    s_vpn = connection.get_setting_vpn()
    return {key: s_vpn.get_secret(key) for key in s_vpn.get_secret_keys()}


@needs_plugin
@pytest.mark.parametrize("link", [REALITY_LINK, XHTTP_LINK, WS_LINK, GRPC_LINK])
@pytest.mark.usefixtures("gtk")
def test_editor_keeps_every_setting(plugin: NM.VpnEditorPlugin, link: str) -> None:
    connection = build_connection(parse_link(link), owner=None)
    result = _round_trip(plugin, connection)
    assert vpn_data(result) == vpn_data(connection)
    assert _secrets(result) == {"uuid": TEST_UUID}


@needs_plugin
@pytest.mark.usefixtures("gtk")
def test_editor_keeps_tunnel_options_and_unknown_keys(plugin: NM.VpnEditorPlugin) -> None:
    tunnel = settings.TunnelOptions(mtu=1400, ipv6=False)
    connection = build_connection(parse_link(REALITY_LINK), owner=None, tunnel=tunnel)
    s_vpn = connection.get_setting_vpn()
    s_vpn.add_data_item("dns", "9.9.9.9")
    s_vpn.add_data_item("future-option", "value")
    result = _round_trip(plugin, connection)
    assert vpn_data(result) == vpn_data(connection)
    assert settings.tunnel_from_items(vpn_data(result)).mtu == 1400


@needs_plugin
@pytest.mark.usefixtures("gtk")
def test_new_connection_defaults(plugin: NM.VpnEditorPlugin) -> None:
    connection = NM.SimpleConnection.new()
    connection.add_setting(NM.SettingVpn(service_type=SERVICE_TYPE))
    editor = plugin.get_editor(connection)
    with pytest.raises(GLib.Error, match="server address is required"):
        editor.update_connection(NM.SimpleConnection.new_clone(connection))


@needs_plugin
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"remove_secret": True}, "user ID is required"),
        ({"reality-public-key": None}, "public key"),
        ({"sni": None}, "server name"),
    ],
)
@pytest.mark.usefixtures("gtk")
def test_editor_validation(
    plugin: NM.VpnEditorPlugin, change: dict[str, Any], message: str
) -> None:
    connection = build_connection(parse_link(REALITY_LINK), owner=None)
    s_vpn = connection.get_setting_vpn()
    for key, value in change.items():
        if key == "remove_secret":
            s_vpn.remove_secret("uuid")
        elif value is None:
            s_vpn.remove_data_item(key)
    editor = plugin.get_editor(connection)
    with pytest.raises(GLib.Error, match=message):
        editor.update_connection(NM.SimpleConnection.new_clone(connection))


def _find(widget: Any, kind: type, predicate: Any = None) -> Any:
    if isinstance(widget, kind) and (predicate is None or predicate(widget)):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _find(child, kind, predicate)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def _wait_for(gtk: Any, condition: Any, timeout: float = 10.0) -> None:
    context = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + int(timeout * 1_000_000)
    while not condition() and GLib.get_monotonic_time() < deadline:
        context.iteration(False) or GLib.usleep(10_000)
    assert condition()


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for nm-vless that records its arguments and input."""
    record = tmp_path / "calls.txt"
    script = tmp_path / "nm-vless"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$*" > {record}\n'
        f"cat >> {record}\n"
        'if [ "$FAKE_FAIL" = 1 ]; then\n'
        '  echo "Provider: skipped line 3: unsupported transport" >&2\n'
        '  echo "nm-vless: provider.example returned HTTP 403" >&2\n'
        "  exit 1\n"
        "fi\n"
        "printf '[subscription]\\nid=abcd\\nname=My provider\\nadded=2\\nupdated=0\\n"
        "unchanged=1\\nremoved=0\\n'\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("NM_VLESS_CLI", str(script))
    return record


@needs_plugin
@pytest.mark.parametrize("fail", [False, True])
def test_editor_adds_subscription(
    plugin: NM.VpnEditorPlugin,
    gtk: Any,
    fake_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
) -> None:
    monkeypatch.setenv("FAKE_FAIL", "1" if fail else "0")
    connection = NM.SimpleConnection.new()
    connection.add_setting(NM.SettingVpn(service_type=SERVICE_TYPE))
    editor = plugin.get_editor(connection)  # keep the editor alive with its widget
    root = editor.get_widget()
    entry = _find(root, gtk.Entry)
    button = _find(root, gtk.Button, lambda b: b.get_label() is not None)
    status = _find(root, gtk.Label, lambda label: not label.get_visible())
    fill_in_label = button.get_label()

    entry.set_text("https://provider.example/sub/token")
    assert button.get_sensitive()
    assert button.get_label() != fill_in_label  # "Add Subscription"
    button.emit("clicked")
    _wait_for(gtk, lambda: status.get_visible() and "…" not in status.get_text())

    assert (
        fake_cli.read_text() == "subscription add - --keyfile\nhttps://provider.example/sub/token"
    )
    if fail:
        assert "provider.example returned HTTP 403" in status.get_text()
        assert status.has_css_class("error")
        assert entry.get_text() == "https://provider.example/sub/token"
    else:
        assert "My provider" in status.get_text()
        assert "3" in status.get_text()
        assert not status.has_css_class("error")
        assert entry.get_text() == ""

    entry.set_text("vless://anything")
    assert button.get_label() == fill_in_label
    entry.set_text("ftp://nope")
    assert not button.get_sensitive()


@needs_plugin
def test_widget_outliving_the_editor_is_safe(plugin: NM.VpnEditorPlugin, gtk: Any) -> None:
    """Signal handlers must not reach a destroyed editor (hosts may keep the widget)."""
    connection = build_connection(parse_link(REALITY_LINK), owner=None)
    root = plugin.get_editor(connection).get_widget()  # the editor is released here
    entry = _find(root, gtk.Entry)
    entry.set_text("https://provider.example/sub")
    entry.emit("activate")


@needs_plugin
@pytest.mark.usefixtures("gtk")
def test_editor_keeps_dns_in_the_tunnel(plugin: NM.VpnEditorPlugin) -> None:
    connection = build_connection(parse_link(REALITY_LINK), owner=None)
    for s_ip in (connection.get_setting_ip4_config(), connection.get_setting_ip6_config()):
        s_ip.props.dns_priority = 0  # like a profile created by GNOME Settings
    result = _round_trip(plugin, connection)
    assert result.get_setting_ip4_config().get_dns_priority() == -50
    assert result.get_setting_ip6_config().get_dns_priority() == -50

    connection.get_setting_ip4_config().props.dns_priority = 10  # chosen by the user
    assert _round_trip(plugin, connection).get_setting_ip4_config().get_dns_priority() == 10
