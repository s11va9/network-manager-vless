#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# Create the nm-vless system user after "meson install". Packages do this in
# their maintainer scripts instead, so nothing runs for staged (DESTDIR) installs.
set -eu

sysusers_file="$1"

if [ -n "${DESTDIR:-}" ]; then
    exit 0
fi
if command -v systemd-sysusers >/dev/null 2>&1; then
    systemd-sysusers "$sysusers_file"
else
    echo "systemd-sysusers not found: create the 'nm-vless' system user manually" >&2
fi
