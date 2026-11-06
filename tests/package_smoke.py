# SPDX-License-Identifier: GPL-2.0-or-later
"""Verify the installed package in a disposable distro container (no live VPN needed)."""

from __future__ import annotations

import configparser
import hashlib
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path

import gi

gi.require_version("NM", "1.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import NM, Gdk, Gtk


def main() -> None:
    registration = Path("/usr/lib/NetworkManager/VPN/nm-vless-service.name")
    config = configparser.ConfigParser()
    config.read(registration)
    service_type = "org.freedesktop.NetworkManager.vless"
    assert config["VPN Connection"]["service"] == service_type
    for program in (config["VPN Connection"]["program"], config["GNOME"]["auth-dialog"]):
        assert os.access(program, os.X_OK), program

    library = Path(config["libnm"]["plugin"])
    plugin = NM.VpnEditorPlugin.load_from_file(str(library), service_type, 0, lambda *_: True, None)
    assert plugin.props.name == "VLESS (Xray)"
    Gtk.init_check()
    assert Gdk.Display.get_default() is not None, "Broadway display did not start"

    # Round-trip a synthetic profile through the installed GTK editor and CLI parser.
    user_id = "b831381d-6324-4d53-ad4f-8cda48b30811"
    with tempfile.TemporaryDirectory() as directory:
        link_file = Path(directory) / "server.txt"
        link_file.write_text(
            f"vless://{user_id}@cdn.example.org:443?security=tls"
            "&sni=cdn.example.org&type=xhttp&path=%2Fxh#Package%20test\n"
        )
        connection = plugin.import_(str(link_file))
        editor = plugin.get_editor(connection)
        result = NM.SimpleConnection.new_clone(connection)
        assert editor.update_connection(result)
        assert result.get_setting_vpn().get_data_item("network") == "xhttp"
        assert result.get_setting_vpn().get_secret("uuid") == user_id

    sys.path.insert(1, "/usr/share/nm-vless")
    from nm_vless import service, xray  # noqa: PLC0415

    assert pwd.getpwnam("nm-vless").pw_uid != 0
    binary = xray.find_xray()
    assert str(binary).startswith("/usr/libexec/nm-vless/")
    assert xray.ensure_supported(binary, prefix=service._privilege_drop_prefix())
    reference = Path(os.environ["NM_VLESS_TEST_XRAY"])
    with binary.open("rb") as installed, reference.open("rb") as pinned:
        assert (
            hashlib.file_digest(installed, "sha256").digest()
            == hashlib.file_digest(pinned, "sha256").digest()
        ), "Packaging modified the verified Xray binary"
    # Exercise the installed auth helper's noninteractive GNOME Shell protocol.
    subprocess.run(
        [
            config["GNOME"]["auth-dialog"],
            "--uuid",
            user_id,
            "--name",
            "Package test",
            "--service",
            service_type,
            "--external-ui-mode",
        ],
        input="DATA_KEY=uuid-flags\nDATA_VAL=0\n\nDONE\n",
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    print("Installed package: editor, CLI, auth helper, unprivileged Xray and checksum OK")


if __name__ == "__main__":
    main()
