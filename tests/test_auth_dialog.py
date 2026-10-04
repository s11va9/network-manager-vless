# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from gi.repository import GLib

from nm_vless.auth_dialog import external_ui, must_ask, read_request

from .conftest import TEST_UUID

ROOT = Path(__file__).resolve().parent.parent
REQUEST = (
    "DATA_KEY=address\nDATA_VAL=vpn.example.com\n\n"
    'DATA_KEY=xhttp-extra\nDATA_VAL={"a":\nDATA_VAL_CONTINUED\n\n'
    "DATA_KEY=uuid-flags\nDATA_VAL=@FLAGS@\n\n"
    f"SECRET_KEY=uuid\nSECRET_VAL={TEST_UUID}\n\n"
    "DONE\n\n"
)


def test_read_request() -> None:
    data, secrets = read_request(REQUEST.replace("@FLAGS@", "0").splitlines(keepends=True))
    assert data == {
        "address": "vpn.example.com",
        "xhttp-extra": '{"a":\nDATA_VAL_CONTINUED',
        "uuid-flags": "0",
    }
    assert secrets == {"uuid": TEST_UUID}


@pytest.mark.parametrize(
    ("flags", "secret", "reprompt", "expected"),
    [
        ("0", TEST_UUID, False, False),  # stored by NetworkManager
        ("0", "", True, False),  # system-owned secrets are never asked for
        ("1", TEST_UUID, False, False),  # agent-owned and known
        ("1", "", False, True),  # agent-owned and missing
        ("1", TEST_UUID, True, True),  # NetworkManager asks for a new one
        ("2", "", False, True),  # not saved
    ],
)
def test_must_ask(flags: str, secret: str, reprompt: bool, expected: bool) -> None:
    secrets = {"uuid": secret} if secret else {}
    assert must_ask({"uuid-flags": flags}, secrets, reprompt=reprompt) is expected


def test_external_ui_keyfile() -> None:
    keyfile = GLib.KeyFile()
    text = external_ui("My VPN", "")
    keyfile.load_from_data(text, len(text.encode()), GLib.KeyFileFlags.NONE)
    assert keyfile.get_integer("VPN Plugin UI", "Version") == 2
    assert "My VPN" in keyfile.get_string("VPN Plugin UI", "Description")
    assert keyfile.get_boolean("uuid", "ShouldAsk")
    assert keyfile.get_boolean("uuid", "IsSecret")


def _run(args: list[str], stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "nm_vless.auth_dialog", "-u", "x", "-n", "My VPN", "-s", "s", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=False,
    )


def test_stored_secret_answers_immediately() -> None:
    """Regression: without a helper GNOME Shell never answered (25 s timeout)."""
    result = _run(["--external-ui-mode", "-i"], REQUEST.replace("@FLAGS@", "0"))
    assert result.returncode == 0
    assert result.stdout == ""  # "every secret is stored"


def test_missing_agent_secret_is_asked_for() -> None:
    result = _run(["--external-ui-mode", "-i"], "DATA_KEY=uuid-flags\nDATA_VAL=1\n\nDONE\n\n")
    assert result.returncode == 0
    assert "ShouldAsk=true" in result.stdout


def test_old_style_protocol() -> None:
    result = _run([], REQUEST.replace("@FLAGS@", "0") + "QUIT\n\n")
    assert result.returncode == 0
    assert result.stdout == "\n\n"
