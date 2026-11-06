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
# Distinct versions and filenames prevent Debian and Ubuntu builds from colliding
# in a release. Runtime library dependencies are still calculated by dpkg-shlibdeps.
if [ -z "${NM_VLESS_DEB_SUFFIX:-}" ]; then
    . /etc/os-release
    case "$ID:$VERSION_ID" in
        debian:13) NM_VLESS_DEB_SUFFIX=deb13 ;;
        ubuntu:*) NM_VLESS_DEB_SUFFIX="ubuntu$VERSION_ID" ;;
        *) echo "Unsupported build host; set NM_VLESS_DEB_SUFFIX explicitly" >&2; exit 1 ;;
    esac
fi
case "$NM_VLESS_DEB_SUFFIX" in
    ''|*[!a-z0-9.]*) echo 'Invalid NM_VLESS_DEB_SUFFIX' >&2; exit 1 ;;
esac
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

tree="$work/network-manager-vless-$version"
mkdir "$tree"
# Copy the sources only: no build trees, caches, virtualenvs or VCS data.
tar -C "$src" \
    --exclude=./.git --exclude=./.venv --exclude=./build --exclude=./stage \
    --exclude=./.aws --exclude=./.codex --exclude=./.agents --exclude='.env*' \
    --exclude=./dist --exclude=./obj-* --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=.mypy_cache --exclude=.ruff_cache \
    --exclude=./BACKLOG.md \
    --exclude=./debian/xray-bundle --exclude=./debian/network-manager-vless \
    -cf - . | tar -C "$tree" -xf -

python3 - "$tree/debian/changelog" "$version" "$NM_VLESS_DEB_SUFFIX" <<'PY'
import sys
from pathlib import Path

path, version, suffix = sys.argv[1:]
changelog = Path(path)
text = changelog.read_text()
changelog.write_text(text.replace(f"({version})", f"({version}+{suffix})", 1))
PY

(cd "$tree" && dpkg-buildpackage --build=binary --unsigned-source --unsigned-changes "$@")

mkdir -p "$src/dist"
find "$work" -maxdepth 1 -name '*.deb' ! -name '*-dbgsym_*' \
    -exec cp '{}' "$src/dist/" \;
ls -1 "$src/dist/"*.deb
