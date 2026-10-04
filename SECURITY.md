# Security policy

NetworkManager-vless runs code as root and handles VPN credentials, so security reports
are taken seriously.

## Reporting a vulnerability

Please report vulnerabilities privately through the repository's security advisory
feature ("Report a vulnerability") rather than a public issue. Include the affected
version, the impact and steps to reproduce. You should receive a reply within a week.

## Scope

In scope: the service (`nm-vless-service`), the command line tool, the generated Xray
configuration, the installed D-Bus, sysusers and systemd files, and the handling of
links, subscriptions and secrets. See [docs/architecture.md](docs/architecture.md) for
the security design.

Out of scope: vulnerabilities in Xray-core, NetworkManager or the VLESS protocol itself;
please report those upstream.

## Supported versions

Only the latest release receives security fixes.
