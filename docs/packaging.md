# Native packages and distribution support

Local results for the prepared release are recorded in
[validation-0.6.0.md](validation-0.6.0.md).

One source tree and upstream version serve every distribution. Packaging differences
belong in `debian/`, `packaging/fedora/` and build options, not in separate forks.

## Targets

| Target | Container | Architectures | Package version / release |
|---|---|---|---|
| Ubuntu 24.04 | `ubuntu:24.04` | amd64, arm64 | `VERSION+ubuntu24.04` |
| Debian 13 | `debian:13` | amd64, arm64 | `VERSION+deb13` |
| Fedora 44 | `registry.fedoraproject.org/fedora:44` | x86_64, aarch64 | `VERSION-1.fc44` |
| Fedora 45 | `registry.fedoraproject.org/fedora:45` | x86_64, aarch64 | `VERSION-1.fc45` |

Build DEBs on their target distribution: `dpkg-shlibdeps` calculates native shared
library dependencies. An Ubuntu build is not a Debian compatibility test. Version
suffixes keep DEB assets distinct in one release; Fedora uses its normal RPM tag.
Debian 12 is outside this matrix.

Both paths read Xray's version, assets and checksums from `build-aux/xray.lock` and
verify the upstream archive before building. RPM receives the verified binary as a
local source; the spec does not download it. Meson strips our C libraries, while RPM
stripping/debug extraction is disabled to preserve Xray. The installed binary must
match the verified input byte for byte.

These are upstream packages, not packages accepted into the official distributions.
Xray's MPL license is included; the pinned source is available at
<https://github.com/XTLS/Xray-core/tree/v26.7.28>.

## Build locally

On Debian 13 or Ubuntu 24.04:

```sh
sudo apt build-dep ./
build-aux/build-deb.sh
```

On Fedora:

```sh
sudo dnf install rpm-build dnf5-plugins curl unzip
build-aux/build-rpm.sh --spec-only > /tmp/NetworkManager-vless.spec
sudo dnf builddep /tmp/NetworkManager-vless.spec
build-aux/build-rpm.sh
```

Packages go into `dist/`. Set `XRAY_ZIP=/path/to/archive.zip` for an offline build
with the same checksum verification.

To reproduce CI, run **one target at a time**:

```sh
mkdir -p /tmp/vless-packages-debian13
docker run --rm --cpus=2 \
  --mount "type=bind,source=$PWD,target=/src,readonly" \
  --mount type=bind,source=/tmp/vless-packages-debian13,target=/output \
  debian:13 sh /src/build-aux/ci-package.sh debian13
```

Substitute the Fedora image, target (`fedora44` / `fedora45`) and a separate output
directory for RPMs. Run `ci-package.sh` only in disposable containers: it installs and
removes packages, and must not be run directly on a workstation.

## CI and releases

CI is the only package builder. After lint/type/service checks, it calls
`.github/workflows/packages.yml` with eight parallel native jobs (`max-parallel: 8`).
Each uses a separate standard GitHub-hosted `ubuntu-24.04` or `ubuntu-24.04-arm`
runner; compilation within a container is limited to two jobs. These runners are
[free for public repositories](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#standard-github-hosted-runners-for-public-repositories).
They do not use the maintainer's workstation. Local container checks remain sequential.

For each target, `ci-package.sh` installs dependencies, requires a working headless
Broadway display and builds the package with pytest against native libraries and the
pinned Xray. Container restrictions may skip nested network-namespace tests; those
also run in the Ubuntu host CI job.

It then installs the package with APT/DNF and runs `tests/package_smoke.py` in isolated
Python mode. The smoke test loads the installed libnm/GTK plugins, imports a synthetic
XHTTP link, saves it through the editor, exercises the auth helper, starts Xray's version
command as `nm-vless` and compares Xray's checksum. Finally it removes the package and
checks that the NetworkManager registration is gone.

Fedora ships a user-unit preset so subscriptions and the GNOME proxy guard have the
same first-install defaults as the DEB. Package checks verify both are enabled for
future sessions; no systemd daemon or desktop session is started inside the container.

Release resolves the tag to its exact commit and reuses the eight artifacts from a
successful CI run for that commit. If branch and tag are pushed together, it waits
for the existing CI run instead of starting another matrix or cancelling checks.
Only same-repository `push` and `workflow_dispatch` runs of `ci.yml` qualify;
pull-request runs and builds of other commits cannot supply release packages.

CI artifacts are retained for seven days. If no CI run or complete unexpired set
exists, Release allows a minute for a concurrent branch push to register, then
dispatches CI once at the version tag and waits. A failed or cancelled CI run blocks
publication: rerun CI, then rerun Release. No personal token is needed; the prepare
job uses `GITHUB_TOKEN` with `actions: write` to dispatch CI, while package jobs have
read-only permissions. CI runs are not automatically cancelled by newer commits.

The publisher downloads the original CI artifacts, requires exactly the eight
expected native package files, generates `SHA256SUMS`, and verifies the tag still
points at the selected commit. It uploads all packages and checksums together. The
workflow summary links the CI run that supplied the packages.

To exercise artifact reuse without publishing or changing an existing release:

```sh
gh workflow run release.yml --ref main -f tag=v0.6.0 -f dry_run=true
```

The manual workflow defaults to a dry run. Use `dry_run=false` only to publish an
existing, unpublished version tag. A dry run can start CI if its artifacts have
expired; it never creates or edits a GitHub Release. `packages.yml` is callable only
from CI, so it cannot accidentally be dispatched as a separate duplicate build.

## Desktop and SELinux validation

Containers cannot establish that VPN traffic, suspend/resume or Fedora's enforcing
SELinux policy work. Fedora remains experimental until these checks pass in a Fedora
Workstation GNOME VM:

- Install the native RPM with SELinux **enforcing** (`getenforce`).
- Import a test link in Settings and connect; verify real HTTP/HTTPS traffic, DNS,
  IPv4/IPv6 routes and the physical route to the upstream server.
- Disconnect, reconnect and suspend/resume. Check that Xray runs as `nm-vless`,
  restarts after failure, and leaves no device or route after disconnect.
- Inspect `sudo ausearch -m AVC,USER_AVC -ts recent`. If necessary, add a narrowly
  scoped, reviewed policy; never disable SELinux or create a blanket allow policy.
- Check package upgrade/removal and both user services after a fresh login.

Run equivalent connection checks on Debian 13 GNOME. Record OS, architecture, desktop
version and results before widening compatibility claims.

## GitHub metadata

README introductions name the product and relevant technologies: VLESS VPN, Linux,
NetworkManager, GNOME, Xray, REALITY, Ubuntu, Debian and Fedora.
`docs/github-metadata.json` holds the GitHub About description and repository topics.
After publishing the first multi-distribution release, a maintainer authenticated with
GitHub CLI can apply them:

```sh
build-aux/update-github-metadata.sh
```

This updates repository topics, not Git release tags; it does not publish code or a
release and is not part of CI. No package adds an APT/DNF repository; repository-based
updates remain deferred.
