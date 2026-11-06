# Validation of the 0.6.0 working tree

Date: 2026-11-07. These are local results, not a claim that a GitHub release or the
remote Actions matrix has already run. Targets were checked **sequentially**, with
Docker limited to two CPUs and compilation to two jobs.

| Environment | Pytest | Installed-package smoke test |
|---|---|---|
| Debian 13, amd64 | 184 passed, 11 skipped | passed |
| Fedora 44, x86_64 | 185 passed, 10 skipped | passed |
| Fedora 45, x86_64 | 185 passed, 10 skipped | passed |
| Host Ubuntu 26.04, Python 3.14 | 178 passed, 17 skipped | not installed on the host |

Each native package was built, installed with APT/DNF, checked with the installed GTK
editor and CLI parser, and removed. Both user units were enabled for future sessions.
Xray ran as `nm-vless` and its installed checksum matched the verified upstream binary.

Containers skip the ten network-namespace service tests; Debian also skips the test
requiring GNOME proxy schemas. Those service tests passed on the host, including real
TUN creation, restart/cleanup and a private D-Bus service. The host skips the seventeen
editor tests because no new editor was built there; those tests passed in all three
native package environments on Broadway.

PyGObject emits known deprecations for its GLib alias loader and asyncio policy bridge
on newer Python/GLib. These specific dependency warnings remain visible. All other
warnings remain errors. The probe's HTTP response leak found on Python 3.15 was fixed
and has a regression test. Immediate normal termination by SIGTERM is accepted by the
TUN cleanup test, rather than requiring Xray to have installed its signal handler in
the same instant it reports readiness.

Static checks: Ruff lint/format, strict mypy, actionlint, shell syntax and
`git diff --check` passed. Native package checks run with `build-aux/ci-package.sh`;
see [packaging.md](packaging.md) for reproduction and the full CI matrix.

Not validated locally: ARM64 packages, the new Ubuntu 24.04 package build, a real
desktop VPN connection on Debian 13, and Fedora Workstation with SELinux enforcing.
The latter remains a separate VM checklist, so Fedora is documented as experimental.
No real VPN connection or package installation on the host was changed during these
checks. APT/DNF repositories were not configured.
