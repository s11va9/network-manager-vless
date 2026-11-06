# SPDX-License-Identifier: GPL-2.0-or-later
"""Release packaging must use the same upstream version and Xray pin."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import nm_vless

ROOT = Path(__file__).resolve().parent.parent


def test_rendered_rpm_uses_upstream_version_and_locked_xray() -> None:
    rendered = subprocess.run(
        ["sh", str(ROOT / "build-aux/build-rpm.sh"), "--spec-only"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    lock = (ROOT / "build-aux/xray.lock").read_text()
    match = re.search(r"^version=v(.+)$", lock, re.MULTILINE)
    assert match is not None
    assert re.search(rf"^Version:\s+{re.escape(nm_vless.__version__)}$", rendered, re.MULTILINE)
    assert f"Provides:       bundled(xray-core) = {match.group(1)}" in rendered
    assert not re.search(r"@[A-Z_]+@", rendered)


def test_release_notes_name_both_native_package_formats() -> None:
    notes = subprocess.run(
        ["sh", str(ROOT / "build-aux/release-notes.sh"), nm_vless.__version__],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for filename in (
        f"network-manager-vless_{nm_vless.__version__}+ubuntu24.04_amd64.deb",
        f"network-manager-vless_{nm_vless.__version__}+deb13_amd64.deb",
        f"NetworkManager-vless-{nm_vless.__version__}-1.fc44.x86_64.rpm",
        f"NetworkManager-vless-{nm_vless.__version__}-1.fc45.x86_64.rpm",
    ):
        assert filename in notes
    assert "SELinux" in notes


def test_release_without_changelog_is_rejected() -> None:
    result = subprocess.run(
        ["sh", str(ROOT / "build-aux/release-notes.sh"), "9999.0.0"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "no section" in result.stderr
