# SPDX-License-Identifier: GPL-2.0-or-later
"""Integration tests of the service with a real TUN device in a private network namespace."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nm_vless import service

ROOT = Path(__file__).resolve().parent.parent
UNSHARE = ["unshare", "--user", "--map-root-user", "--net"]


def _netns_available() -> bool:
    if shutil.which("unshare") is None:
        return False
    probe = subprocess.run(
        [*UNSHARE, "ip", "tuntap", "show"], capture_output=True, check=False, timeout=10
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _netns_available(), reason="unprivileged network namespaces are not available"
)


def _run_session(xray_binary: Path, watch: int = 0) -> tuple[dict[str, Any], str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    extra = [str(watch)] if watch else []
    completed = subprocess.run(
        [
            *UNSHARE,
            sys.executable,
            str(ROOT / "tests" / "netns_session.py"),
            str(xray_binary),
            *extra,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1]), completed.stderr


def test_failure_is_reported_with_xray_message(tmp_path: Path) -> None:
    fake = tmp_path / "xray"
    fake.write_text('#!/bin/sh\necho "Failed to start: main: bad config"\nexit 23\n')
    fake.chmod(0o755)

    result, log = _run_session(fake)

    assert result["events"] == ["failure"]
    assert "xray exited with status 23: Failed to start: main: bad config" in log
    assert result["tun_exists_after"] is False


_XRAY = os.environ.get("NM_VLESS_TEST_XRAY") or shutil.which("xray")


@pytest.mark.skipif(_XRAY is None, reason="set NM_VLESS_TEST_XRAY to run xray with a TUN device")
def test_xray_accepts_tun_fd_and_cleans_up() -> None:
    assert _XRAY is not None
    result, log = _run_session(Path(_XRAY))

    assert result["events"] == ["config", "ip4", "ip6"], log
    assert result["tundev"].startswith("vless")
    assert result["tun_exists_while_connected"] is True
    assert result["tun_exists_after"] is False
    assert "xray exited with status 0" in log


@pytest.mark.skipif(
    _XRAY is None or shutil.which("dbus-run-session") is None,
    reason="needs dbus-run-session and NM_VLESS_TEST_XRAY",
)
def test_dbus_protocol_as_used_by_network_manager() -> None:
    """NeedSecrets, rejected Connect, Connect with signals and Disconnect over D-Bus."""
    assert _XRAY is not None
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    script = (
        f"ip link set lo up && dbus-run-session -- {sys.executable} "
        f"{ROOT / 'tests' / 'dbus_session.py'} {_XRAY}"
    )
    completed = subprocess.run(
        [*UNSHARE, "sh", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=True,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    # need_secrets is not overridden (see VlessPlugin): libnm answers "none needed".
    assert result["need_secrets"] == ""
    assert result["invalid_error"] == "org.freedesktop.NetworkManager.VPN.Error.BadArguments"
    assert result["invalid_exit"] == 0
    assert result["missing_secret_error"] == "org.freedesktop.NetworkManager.VPN.Error.BadArguments"
    assert result["signals"] == ["Config", "Ip4Config", "Ip6Config"], completed.stderr
    # NMVpnServiceState: STARTING, STARTED, STOPPING, STOPPED
    assert result["states"] == [3, 4, 5, 6]
    assert result["tun_while_connected"] is True
    assert result["tun_after_disconnect"] is False


#: CapabilityBoundingSet of NetworkManager.service, which also bounds its VPN services.
NM_CAPABILITIES = (
    "-all,+net_admin,+dac_override,+net_raw,+net_bind_service,+setgid,+setuid,"
    "+sys_module,+audit_write,+kill,+sys_chroot"
)


def _prefix_as_root_of_nm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr("nm_vless.service.os.geteuid", lambda: 0)
    monkeypatch.setattr("nm_vless.service.pwd.getpwnam", lambda _name: object())
    monkeypatch.setattr("nm_vless.service.shutil.which", lambda *_a, **_k: "/usr/bin/setpriv")
    return service._privilege_drop_prefix()


def test_privilege_drop_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = _prefix_as_root_of_nm(monkeypatch)
    assert prefix[0] == "/usr/bin/setpriv"
    assert prefix[-1] == "--"
    for option in ("--reuid=nm-vless", "--regid=nm-vless", "--clear-groups", "--no-new-privs"):
        assert option in prefix


def test_privilege_drop_works_with_network_manager_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: --bounding-set needs CAP_SETPCAP, which NetworkManager.service lacks."""
    prefix = _prefix_as_root_of_nm(monkeypatch)
    # Inside the namespace only uid 0 exists and setgroups() is denied, so switch to
    # uid 0 and keep the groups; every option that touches capabilities is kept.
    options = [
        "--reuid=0"
        if arg.startswith("--reuid=")
        else "--keep-groups"
        if arg == "--clear-groups"
        else arg
        for arg in prefix[1:]
        if not arg.startswith("--regid=")
    ]
    completed = subprocess.run(
        [
            *UNSHARE,
            "setpriv",
            f"--bounding-set={NM_CAPABILITIES}",
            "--",
            "setpriv",
            *options,
            "true",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


CRASHING_XRAY = """#!/bin/sh
# Reports readiness like xray, then exits for the first @CRASHES@ runs. The service
# starts xray with a clean environment, so the settings are part of the script.
n=$(cat "@COUNTER@" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "@COUNTER@"
echo "Xray 26.7.28 started"
if [ "$n" -le @CRASHES@ ]; then sleep 0.3; echo "panic: test crash $n"; exit 2; fi
exec sleep 60
"""


def _crashing_xray(tmp_path: Path, crashes: int) -> Path:
    fake = tmp_path / "xray"
    script = CRASHING_XRAY.replace("@CRASHES@", str(crashes))
    fake.write_text(script.replace("@COUNTER@", str(tmp_path / "count")))
    fake.chmod(0o755)
    return fake


def test_xray_is_restarted_after_crashes(tmp_path: Path) -> None:
    result, log = _run_session(_crashing_xray(tmp_path, crashes=2), watch=6)

    # NetworkManager is configured once; the restarts are invisible to it.
    assert result["events"] == ["config", "ip4", "ip6"], log
    assert log.count("restarting in 1 s") == 2
    assert "panic: test crash 2" in log
    assert "xray is running again" in log
    assert (tmp_path / "count").read_text().strip() == "3"


def test_crash_loop_fails_the_connection(tmp_path: Path) -> None:
    result, log = _run_session(_crashing_xray(tmp_path, crashes=100), watch=15)

    assert result["events"] == ["config", "ip4", "ip6", "failure"], log
    assert log.count("restarting in 1 s") == service.MAX_RESTARTS
    assert result["tun_exists_after"] is False


def test_logs_go_to_syslog_when_network_manager_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    # NetworkManager discards the output of VPN services and sets NM_VPN_LOG_SYSLOG=1.
    monkeypatch.setenv("NM_VPN_LOG_SYSLOG", "1")
    if Path("/dev/log").exists():
        handler = service._log_handler(debug=False)
        assert isinstance(handler, logging.handlers.SysLogHandler)
        assert handler.ident.startswith("nm-vless-service[")
        handler.close()
    # --debug and manual runs keep using stderr.
    assert type(service._log_handler(debug=True)) is logging.StreamHandler
    monkeypatch.delenv("NM_VPN_LOG_SYSLOG")
    assert type(service._log_handler(debug=False)) is logging.StreamHandler


def test_xray_output_levels() -> None:
    assert service._xray_line_level("2026/09/29 [Error] failed") == logging.ERROR
    assert service._xray_line_level("2026/09/29 [Warning] core: Xray started") == logging.WARNING
    assert service._xray_line_level("2026/09/29 [Info] tunneling request") == logging.INFO
    assert service._xray_line_level("Xray 26.7.28 (Xray, Penetrates Everything.)") == logging.INFO


def test_xray_logs_connections_only_when_debugging() -> None:
    previous = service.LOG.level
    try:
        service.LOG.setLevel(logging.INFO)
        assert service._xray_log_level() == "warning"
        service.LOG.setLevel(logging.DEBUG)
        assert service._xray_log_level() == "info"
    finally:
        service.LOG.setLevel(previous)
