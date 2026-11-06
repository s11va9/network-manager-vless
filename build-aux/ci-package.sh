#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# Build, install and smoke-test a native package inside a disposable container.
# Mount the checkout read-only at /src and a writable artifact directory at /output.
set -eu
export DEB_BUILD_OPTIONS=parallel=2

target=${1:?usage: ci-package.sh ubuntu24.04|debian13|fedora44|fedora45}
if ! command -v tar > /dev/null 2>&1; then
    dnf install -y --setopt=install_weak_deps=False tar gzip
fi
work=$(mktemp -d)
trap 'test -z "${broadway_pid:-}" || kill "$broadway_pid"; rm -rf "$work"' EXIT
mkdir "$work/source"
tar -C /src \
    --exclude=./.git --exclude=./.venv --exclude=./build --exclude=./stage \
    --exclude=./.aws --exclude=./.codex --exclude=./.agents --exclude='.env*' \
    --exclude=./dist --exclude=./obj-* --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=.mypy_cache --exclude=.ruff_cache --exclude=./BACKLOG.md \
    --exclude=./debian/xray-bundle --exclude=./debian/network-manager-vless \
    -cf - . | tar -C "$work/source" -xf -
cd "$work/source"
. /etc/os-release

case "$target:$ID:$VERSION_ID" in
    ubuntu24.04:ubuntu:24.04|debian13:debian:13)
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y --no-install-recommends ca-certificates
        apt-get build-dep -y --no-install-recommends .
        apt-get install -y --no-install-recommends libgtk-4-bin dbus iproute2
        ;;
    fedora44:fedora:44|fedora45:fedora:45)
        dnf install -y --setopt=install_weak_deps=False rpm-build dnf5-plugins tar gzip findutils diffutils patch \
            curl unzip ca-certificates
        build-aux/build-rpm.sh --spec-only > "$work/NetworkManager-vless.spec"
        dnf builddep -y --setopt=install_weak_deps=False "$work/NetworkManager-vless.spec"
        dnf install -y --setopt=install_weak_deps=False gtk4-devel-tools dbus-daemon iproute
        ;;
    *) echo "Unexpected container $ID $VERSION_ID for $target" >&2; exit 1 ;;
esac

case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64) arch=arm64 ;;
    *) echo 'Unsupported architecture' >&2; exit 1 ;;
esac
build-aux/fetch-xray.sh "$arch" "$work/xray"
export XRAY_ZIP="$work/xray/$(sed -n "s/^$arch=\([^ ]*\).*/\1/p" build-aux/xray.lock)"
export NM_VLESS_TEST_XRAY="$work/xray/xray"
export GDK_BACKEND=broadway BROADWAY_DISPLAY=:5
export XDG_RUNTIME_DIR="$work/runtime"
export NM_VLESS_BROADWAY_RUNTIME_DIR="$XDG_RUNTIME_DIR"
mkdir -m 0700 "$XDG_RUNTIME_DIR"
broadway=$(command -v gtk4-broadwayd || true)
if [ -z "$broadway" ]; then broadway=/usr/libexec/gtk4-broadwayd; fi
test -x "$broadway"
"$broadway" :5 > "$work/broadway.log" 2>&1 &
broadway_pid=$!
python3 - <<'PY'
import time
import gi
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk
Gtk.init_check()
for _ in range(50):
    display = Gdk.Display.open(":5")
    if display is not None:
        display.close()
        break
    time.sleep(0.1)
else:
    raise RuntimeError("GTK Broadway display is unavailable; UI tests must not be skipped")
PY

case "$target" in
    ubuntu*|debian*)
        build-aux/build-deb.sh
        apt-get install -y --no-install-recommends ./dist/*.deb
        ;;
    fedora*)
        build-aux/build-rpm.sh
        dnf install -y --setopt=install_weak_deps=False ./dist/*.rpm
        ;;
esac

# Import and validate the installed libraries, not only the ones in the build tree.
systemctl --global is-enabled nm-vless-subscriptions.timer
systemctl --global is-enabled nm-vless-proxy-guard.service
python3 -I tests/package_smoke.py
nm-vless --help > /dev/null

# Package removal must remove the plugin registration as well.
case "$target" in
    ubuntu*|debian*) apt-get remove -y network-manager-vless ;;
    fedora*) dnf remove -y NetworkManager-vless ;;
esac
test ! -e /usr/lib/NetworkManager/VPN/nm-vless-service.name

mkdir -p /output
cp dist/* /output/
