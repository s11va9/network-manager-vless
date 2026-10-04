#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Build the Debian/Ubuntu package from a clean copy of the source tree and put
# the result into dist/.
#
#   sudo apt build-dep ./        # once: install the build dependencies
#   build-aux/build-deb.sh
#
# Set XRAY_ZIP to a downloaded Xray-core release archive for offline builds.
set -eu

src=$(cd "$(dirname "$0")/.." && pwd)
version=$(sed -n '1s/.*(\(.*\)).*/\1/p' "$src/debian/changelog")
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

tree="$work/network-manager-vless-$version"
mkdir "$tree"
# Copy the sources only: no build trees, caches, virtualenvs or VCS data.
tar -C "$src" \
    --exclude=./.git --exclude=./.venv --exclude=./build --exclude=./stage \
    --exclude=./dist --exclude=./obj-* --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=.mypy_cache --exclude=.ruff_cache \
    --exclude=./debian/xray-bundle --exclude=./debian/network-manager-vless \
    -cf - . | tar -C "$tree" -xf -

(cd "$tree" && dpkg-buildpackage --build=binary --unsigned-source --unsigned-changes "$@")

mkdir -p "$src/dist"
cp "$work"/*.deb "$src/dist/"
ls -1 "$src/dist/"*.deb
