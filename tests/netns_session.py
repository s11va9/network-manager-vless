# SPDX-License-Identifier: GPL-2.0-or-later
"""Run one service Session inside a private network namespace and print its events as JSON.

Used by test_service.py through ``unshare --user --map-root-user --net``, which grants
CAP_NET_ADMIN for TUN devices without real root. NetworkManager is replaced by a
recorder; the TUN device and the xray process are real.

Usage: netns_session.py XRAY_BINARY [WATCH_SECONDS]

Without WATCH_SECONDS the session stops as soon as it is configured. With it, the
session keeps running for that long (to observe restarts) and then stops.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from gi.repository import GLib

from nm_vless import service, settings
from nm_vless.link import parse_link

UUID = "b831381d-6324-4d53-ad4f-8cda48b30811"
LINK = f"vless://{UUID}@203.0.113.10:443?security=tls&sni=example.com&type=xhttp&path=/x#t"


def _link_exists(name: str) -> bool:
    return (
        subprocess.run(["ip", "link", "show", name], capture_output=True, check=False).returncode
        == 0
    )


def main() -> int:
    # Only uid 0 exists in the namespace; dropping privileges is tested separately.
    service._privilege_drop_prefix = list
    setattr(service, "RESTART_DELAY_S", 1)  # noqa: B010 - keep the tests fast
    watch = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    loop = GLib.MainLoop()
    result: dict[str, Any] = {"events": []}

    class Recorder:
        def set_config(self, variant: GLib.Variant) -> None:
            config = variant.unpack()
            result["events"].append("config")
            result["tundev"] = config["tundev"]
            result["tun_exists_while_connected"] = _link_exists(config["tundev"])

        def set_ip4_config(self, _variant: GLib.Variant) -> None:
            result["events"].append("ip4")

        def set_ip6_config(self, _variant: GLib.Variant) -> None:
            result["events"].append("ip6")
            if not watch:
                session.stop()
                GLib.timeout_add(1000, loop.quit)

        def report_failure(self) -> None:
            result["events"].append("failure")
            GLib.idle_add(loop.quit)

    session = service.Session(
        Recorder(),  # type: ignore[arg-type]
        parse_link(LINK),
        settings.TunnelOptions(),
        Path(sys.argv[1]),
    )
    session.start()
    if watch:

        def finish() -> bool:
            session.stop()
            GLib.timeout_add(1000, loop.quit)
            return False

        GLib.timeout_add_seconds(watch, finish)
    GLib.timeout_add_seconds(max(25, watch + 5), loop.quit)
    loop.run()
    tundev = result.get("tundev", "vless0")
    result["tun_exists_after"] = _link_exists(tundev)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
