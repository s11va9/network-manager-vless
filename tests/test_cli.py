# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import pytest
from gi.repository import GLib

from nm_vless import cli
from nm_vless.cli import report_to_keyfile
from nm_vless.subscription import Subscription
from nm_vless.sync import SyncReport


def test_report_to_keyfile() -> None:
    sub = Subscription(
        id="abcd1234", url="https://e.com/s", name="Мой VPN", update_interval_hours=12
    )
    report = SyncReport(added=["a", "b"], unchanged=["c"], kept_active=["d"])
    text = report_to_keyfile(sub, report)
    keyfile = GLib.KeyFile()
    keyfile.load_from_data(text, len(text.encode()), GLib.KeyFileFlags.NONE)
    assert keyfile.get_string("subscription", "name") == "Мой VPN"
    assert keyfile.get_integer("subscription", "added") == 2
    assert keyfile.get_integer("subscription", "unchanged") == 1
    assert keyfile.get_integer("subscription", "kept-active") == 1
    assert keyfile.get_integer("subscription", "update-interval-hours") == 12
    assert "https://" not in text  # the URL is a credential


def test_test_command_only_accepts_https(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["test", "Server", "http://example.org/"]) == 1
    assert "only https:// URLs" in capsys.readouterr().err
