# SPDX-License-Identifier: GPL-2.0-or-later
"""Authentication helper that secret agents run for VLESS connections.

GNOME Shell and other secret agents start the helper named in the ``[GNOME]
auth-dialog`` key of the plugin's .name file whenever NetworkManager asks them for
VPN secrets, for example when GNOME Settings opens a connection. Without a helper,
GNOME Shell never answers and every request waits for NetworkManager's 25-second
timeout.

The user id is normally stored by NetworkManager (secret flags 0), so the helper
answers "nothing to ask" immediately. It only asks for the id when it is owned by
the agent or not saved, and is missing or has to be entered again.

Protocols (see ``VPNRequestHandler`` in GNOME Shell's networkAgent.js):

* input on stdin (see :func:`read_request`): ``DATA_KEY=``/``DATA_VAL=`` and
  ``SECRET_KEY=``/``SECRET_VAL=`` pairs, terminated by ``DONE``;
* ``--external-ui-mode``: print a GKeyFile describing what to ask (empty output means
  every secret is stored) and exit;
* otherwise: print ``key``/``value`` line pairs, then an empty line, and wait for
  ``QUIT`` on stdin.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Mapping
from typing import Final

import gi

gi.require_version("NM", "1.0")
from gi.repository import NM, GLib

from nm_vless import settings
from nm_vless.buildinfo import translation

UI_GROUP: Final = "VPN Plugin UI"
_ASK_FLAGS: Final = int(NM.SettingSecretFlags.AGENT_OWNED | NM.SettingSecretFlags.NOT_SAVED)


_ = translation().gettext


def read_request(lines: Iterable[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Parse the connection details sent by the agent, up to ``DONE``.

    Implemented here because PyGObject cannot convert the hash tables returned by
    libnm's ``nm_vpn_service_plugin_read_vpn_details()``. As there, a value continues
    until the next empty line.
    """
    data: dict[str, str] = {}
    secrets: dict[str, str] = {}
    key: str | None = None
    target = data
    value: list[str] | None = None

    def finish() -> None:
        nonlocal value
        if key is not None and value is not None:
            target[key] = "\n".join(value)
        value = None

    for raw in lines:
        line = raw.rstrip("\n")
        if value is not None:
            if line:
                value.append(line)
                continue
            finish()
            continue
        if line == "DONE":
            break
        if line.startswith("DATA_KEY="):
            key, target = line[len("DATA_KEY=") :], data
        elif line.startswith("SECRET_KEY="):
            key, target = line[len("SECRET_KEY=") :], secrets
        elif line.startswith(("DATA_VAL=", "SECRET_VAL=")):
            value = [line.split("=", 1)[1]]
    finish()
    return data, secrets


def must_ask(data: Mapping[str, str], secrets: Mapping[str, str], *, reprompt: bool) -> bool:
    """Whether the user has to enter the user id."""
    try:
        flags = int(data.get(f"{settings.SECRET_UUID}-flags", "0") or 0)
    except ValueError:
        flags = 0
    if not flags & _ASK_FLAGS:
        return False  # stored by NetworkManager, never asked for
    return reprompt or not secrets.get(settings.SECRET_UUID)


def external_ui(name: str, current: str) -> str:
    """GKeyFile that asks GNOME Shell to show a password dialog for the user id."""
    keyfile = GLib.KeyFile()
    keyfile.set_integer(UI_GROUP, "Version", 2)
    keyfile.set_string(UI_GROUP, "Title", _("Authenticate VPN"))
    keyfile.set_string(
        UI_GROUP, "Description", _("Enter the VLESS user ID for “{name}”.").format(name=name)
    )
    keyfile.set_string(settings.SECRET_UUID, "Value", current)
    keyfile.set_string(settings.SECRET_UUID, "Label", _("User ID"))
    keyfile.set_boolean(settings.SECRET_UUID, "IsSecret", True)
    keyfile.set_boolean(settings.SECRET_UUID, "ShouldAsk", True)
    text, _length = keyfile.to_data()
    return str(text)


def _wait_for_quit() -> None:
    for line in sys.stdin:
        if line.strip() == "QUIT":
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VLESS VPN authentication helper")
    parser.add_argument("-u", "--uuid", required=True, help="connection UUID")
    parser.add_argument("-n", "--name", required=True, help="connection name")
    parser.add_argument("-s", "--service", required=True, help="VPN service type")
    parser.add_argument("-i", "--allow-interaction", action="store_true")
    parser.add_argument("-r", "--reprompt", action="store_true")
    parser.add_argument("-t", "--hint", action="append", default=[])
    parser.add_argument("--external-ui-mode", action="store_true")
    args = parser.parse_args(argv)

    # Read the whole request first: the agent treats an early exit as an error.
    data, secrets = read_request(sys.stdin)
    ask = must_ask(data, secrets, reprompt=args.reprompt)

    if args.external_ui_mode:
        if ask and args.allow_interaction:
            sys.stdout.write(external_ui(args.name, secrets.get(settings.SECRET_UUID, "")))
        return 0

    # Old-style agents (nm-applet) expect the helper to show its own window; the id
    # is stored by NetworkManager in every configuration this plugin creates.
    if ask:
        print("nm-vless-auth-dialog: interactive input needs --external-ui-mode", file=sys.stderr)
        return 1
    sys.stdout.write("\n\n")
    sys.stdout.flush()
    _wait_for_quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
