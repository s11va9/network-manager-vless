# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-10-03

### Added

- `nm-vless-proxy-guard.service` (user session): while a VLESS connection is up, a
  GNOME system proxy that points to another client's local proxy (for example Happ's
  `127.0.0.1:10809`) is turned off, with a notification, and turned back on when VLESS
  disconnects. Browsers and Electron applications such as Claude Desktop now use the
  tunnel instead of the other client's proxy.

### Fixed

- Traffic went to another client's TUN device while it was up (Happ's default route
  has metric 1, below the VPN's 50). The tunnel now also routes `0.0.0.0/1` and
  `128.0.0.0/1` (`::/1` and `8000::/1`), which win over any default route. Profiles
  with "Use this connection only for resources on its network" are unchanged.
- Subscription updates created every server again on Ubuntu: netplan, which stores
  NetworkManager profiles there, loses the `user` setting that linked a profile to its
  subscription ("keyfile: user.nm-vless: invalid setting name"). The link is now stored
  in `vpn.data` (`subscription`, `server-key`); the first update keeps the copy that was
  used last and deletes the other copies unless they are connected.
- `nm-vless check` no longer reports another client's default route, which cannot win
  any more, and does not report the system proxy while the proxy guard is running.

## [0.4.0] - 2026-09-28

### Added

- Xray-core is restarted automatically (after 2 seconds, on the same tunnel) if it
  exits while the connection is up; NetworkManager is only told about a failure after
  more than 3 exits in 5 minutes.
- `nm-vless check` points out profiles whose DNS queries may leave the tunnel and
  prints the command that fixes them.
- `nm-vless check` warns about `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY` variables
  that point to a local proxy.
- `nm-vless test CONNECTION [URL...]` checks which sites open through a server and
  where its traffic leaves, without connecting the VPN.
- Subscription updates move existing profiles with NetworkManager's default DNS
  priority into the tunnel, as the editor does when saving.

### Fixed

- Sites that the server routes by domain (for example through a separate exit) failed
  with "connection closed" (`ERR_CONNECTION_CLOSED`) while other sites worked: Xray now
  sends the server the domain of each connection (sniffing TLS, HTTP and QUIC), as
  other Xray clients do, instead of only the IP address.
- Service and xray messages reach the journal (`journalctl -t nm-vless-service`);
  NetworkManager discards the output of VPN services. With NetworkManager's
  `VPN_PLUGIN:DEBUG` logging, xray also logs every connection it fails to forward.
- Opening a VLESS connection in GNOME Settings (the ⚙ button) took 25 seconds: GNOME
  Shell's secret agent needs an authentication helper for every VPN type and never
  answered without one. The new `nm-vless-auth-dialog` answers immediately when the
  user id is stored and asks for it in the standard dialog otherwise.

### Security

- DNS queries stay inside the tunnel: new profiles, and profiles saved in the editor
  with the default priority, use `dns-priority -50`, so systemd-resolved no longer also
  asks the DNS server of the physical network.

## [0.3.0] - 2026-09-28

### Added

- Subscriptions in GNOME Settings: paste an `https://` subscription URL into
  "Import from link" and click "Add Subscription"; every server becomes a VPN
  connection and is kept up to date automatically.
- Each subscription is updated at the interval requested by the provider
  (`profile-update-interval`, 6 hours by default); the timer now runs hourly and
  `nm-vless subscription update --due` only downloads subscriptions that are due.
- `nm-vless check` warns about other tunnels with a default route and about a system
  proxy that points to another client's local proxy; the service logs the same
  warning for tunnels when connecting.

### Fixed

- Adding a subscription starts the update timer right away instead of at the next
  login.
- Editor signal handlers are disconnected when the editor is destroyed.
- A small memory leak when importing a link in the editor.

## [0.2.1] - 2026-09-28

### Fixed

- Connections failed with "cannot run /usr/libexec/nm-vless/xray … exit status 127":
  the service asked `setpriv` to change the capability bounding set, which needs
  `CAP_SETPCAP`, and NetworkManager.service does not grant it. Dropping to the
  `nm-vless` user already removes all capabilities.
- Errors from running xray now include its message instead of only the exit status.

## [0.2.0] - 2026-09-28

### Added

- GNOME Settings integration: "VLESS (Xray)" in the Add VPN dialog, an editor for all
  connection settings with "fill in from a vless:// link", and import of files
  containing a link. English and Russian user interface.
- Debian/Ubuntu package that bundles a verified Xray-core, so installation is a
  single `.deb`.
- `nm-vless parse-link` prints the settings of a link (used by the editor).

### Changed

- The service prefers the Xray-core shipped with the package over a system-wide one.

### Fixed

- The service no longer overrides `need_secrets`, whose introspection data is wrong in
  libnm and crashed the service on the first connection attempt.

## [0.1.0] - 2026-09-28

### Added

- NetworkManager VPN service `nm-vless-service` for VLESS over Xray-core with a
  TUN device: TCP (with XTLS Vision), WebSocket, HTTPUpgrade, gRPC and XHTTP
  transports; TLS and REALITY security.
- Privilege separation: the service creates the TUN device and runs Xray as the
  unprivileged `nm-vless` user via `XRAY_TUN_FD`.
- Full tunnel for IPv4 and IPv6 with DNS servers inside the tunnel; routing loops
  are prevented by NetworkManager's route to the external gateway.
- `nm-vless` command line tool: import and export `vless://` links, list
  connections, manage subscriptions, check the installation.
- Subscriptions: HTTPS download, base64 and plain text formats, provider
  metadata, synchronisation that preserves user changes, systemd user timer.
