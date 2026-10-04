#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Print the GitHub release description for VERSION: installation instructions and
# the CHANGELOG.md section of that version.
#
#   build-aux/release-notes.sh 0.5.0 > notes.md
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

Download the package for your architecture below (\`amd64\` for most PCs, \`arm64\`
for ARM devices) and open it, or install it from a terminal:

\`\`\`sh
sudo apt install ./network-manager-vless_${version}_amd64.deb
\`\`\`

Then add a connection in **Settings → Network → VPN → + → VLESS (Xray)**. Ubuntu 24.04
or newer and other distributions with NetworkManager 1.24+ are supported; see the
[README]($repo/blob/v${version}/README.md) ([на русском]($repo/blob/v${version}/README.ru.md)).
If GNOME Settings was open during an update, restart it.

Verify a download with \`sha256sum -c SHA256SUMS --ignore-missing\`.

## Changes
$changes
NOTES
