#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Print the GitHub release description for VERSION: installation instructions and
# the CHANGELOG.md section of that version.
#
#   build-aux/release-notes.sh 0.6.0 > notes.md
set -eu

version=${1:?usage: $0 VERSION}
repo=https://github.com/${GITHUB_REPOSITORY:-s11va9/network-manager-vless}
src=$(cd "$(dirname "$0")/.." && pwd)

changes=$(awk -v heading="## [$version]" '
    index($0, heading) == 1 { found = 1; next }
    found && /^## \[/ { exit }
    found { print }
' "$src/CHANGELOG.md")
if [ -z "$(printf '%s' "$changes" | tr -d '[:space:]')" ]; then
    echo "$0: CHANGELOG.md has no section for $version" >&2
    exit 1
fi

cat <<NOTES
## Installation

Choose the package for your distribution and architecture:

| System | x86-64 / Intel / AMD | ARM64 |
|---|---|---|
| Ubuntu 24.04 | \`network-manager-vless_${version}+ubuntu24.04_amd64.deb\` | same, with \`arm64\` |
| Debian 13 (Trixie) | \`network-manager-vless_${version}+deb13_amd64.deb\` | same, with \`arm64\` |
| Fedora 44 | \`NetworkManager-vless-${version}-1.fc44.x86_64.rpm\` | same, with \`aarch64\` |
| Fedora 45 | \`NetworkManager-vless-${version}-1.fc45.x86_64.rpm\` | same, with \`aarch64\` |

Ubuntu / Debian: open the matching \`.deb\`, or run
\`sudo apt install ./network-manager-vless_*.deb\` (keep only the intended package in the directory).
Fedora Workstation: open the matching \`.rpm\`, or run
\`sudo dnf install ./NetworkManager-vless-*.rpm\`.

Then add a connection in **Settings → Network → VPN → + → VLESS (Xray)**.
See the [README]($repo/blob/v${version}/README.md)
([на русском]($repo/blob/v${version}/README.ru.md)) for subscriptions and compatibility.
If GNOME Settings was open during an update, restart it.

All packages are built and installation-tested in CI. Fedora's SELinux-enforcing
VPN connection check still requires a Workstation VM; see the compatibility notes.
Packages do not add an APT/DNF repository; install a later release over the old one.

Verify a download with \`sha256sum -c SHA256SUMS --ignore-missing\`.

## Changes
$changes
NOTES
