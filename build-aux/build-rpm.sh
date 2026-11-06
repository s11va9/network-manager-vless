#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# Build on Fedora, with dependencies from packaging/fedora/NetworkManager-vless.spec.in.
# The pinned Xray release is fetched and verified BEFORE rpmbuild, which is offline.
# Pass --spec-only to print a rendered spec (for dnf builddep).
set -eu

src=$(cd "$(dirname "$0")/.." && pwd)
version=$(sed -n 's/^__version__: Final = "\(.*\)"/\1/p' "$src/src/nm_vless/__init__.py")
xray_version=$(sed -n 's/^version=v//p' "$src/build-aux/xray.lock")
release_date=$(sed -n '/^ -- /{s/^ -- .*>  //;p;q;}' "$src/debian/changelog")
changelog_date=$(LC_ALL=C date -u -d "$release_date" '+%a %b %d %Y')
render_spec() {
    sed -e "s/@VERSION@/$version/g" -e "s/@XRAY_VERSION@/$xray_version/g" \
        -e "s/@CHANGELOG_DATE@/$changelog_date/g" \
        "$src/packaging/fedora/NetworkManager-vless.spec.in"
}
if [ "${1:-}" = --spec-only ]; then
    render_spec
    exit 0
fi
if [ "$#" -ne 0 ]; then
    echo "usage: $0 [--spec-only]" >&2
    exit 1
fi
case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64) arch=arm64 ;;
    *) echo 'Only x86_64 and aarch64 are supported' >&2; exit 1 ;;
esac

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/SOURCES" "$work/SPECS"
"$src/build-aux/fetch-xray.sh" "$arch" "$work/xray-bundle"
cp "$work/xray-bundle/xray" "$work/xray-bundle/xray-LICENSE" "$work/SOURCES/"
render_spec > "$work/SPECS/NetworkManager-vless.spec"
tar -C "$src" \
    --exclude=./.git --exclude=./.venv --exclude=./build --exclude=./stage \
    --exclude=./.aws --exclude=./.codex --exclude=./.agents --exclude='.env*' \
    --exclude=./dist --exclude=./obj-* --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=.mypy_cache --exclude=.ruff_cache --exclude=./BACKLOG.md \
    --exclude=./debian/xray-bundle --exclude=./debian/network-manager-vless \
    --transform="s,^\.,network-manager-vless-$version," \
    -czf "$work/SOURCES/network-manager-vless-$version.tar.gz" .
rpmbuild -bb --define "_topdir $work" --define '_smp_build_ncpus 2' \
    "$work/SPECS/NetworkManager-vless.spec"

mkdir -p "$src/dist"
find "$work/RPMS" -name '*.rpm' -exec cp '{}' "$src/dist/" \;
ls -1 "$src/dist/"*.rpm
