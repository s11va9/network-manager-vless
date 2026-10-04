#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Download the Xray-core release pinned in build-aux/xray.lock, verify its checksum
# and extract the binary and its license into DEST.
#
# Usage: fetch-xray.sh DEB_ARCH DEST
# Set XRAY_ZIP=/path/to/Xray-linux-*.zip to use an already downloaded archive
# (offline builds); it is verified against the same checksum.
set -eu

arch=$1
dest=$2
lock="$(dirname "$0")/xray.lock"

version=$(sed -n 's/^version=//p' "$lock")
entry=$(sed -n "s/^$arch=//p" "$lock")
if [ -z "$entry" ]; then
    echo "fetch-xray: no Xray-core build pinned for architecture '$arch'" >&2
    exit 1
fi
asset=${entry% *}
checksum=${entry#* }

mkdir -p "$dest"
zip="$dest/$asset"
if [ -n "${XRAY_ZIP:-}" ]; then
    cp "$XRAY_ZIP" "$zip"
elif [ ! -f "$zip" ]; then
    url="https://github.com/XTLS/Xray-core/releases/download/$version/$asset"
    echo "fetch-xray: downloading $url"
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 -o "$zip" "$url"
fi

echo "$checksum  $zip" | sha256sum --check --status || {
    echo "fetch-xray: checksum mismatch for $asset, refusing to use it" >&2
    rm -f "$zip"
    exit 1
}

unzip -o -q "$zip" xray LICENSE -d "$dest"
mv "$dest/LICENSE" "$dest/xray-LICENSE"
chmod 0755 "$dest/xray"
echo "fetch-xray: Xray-core $version ($arch) verified"
