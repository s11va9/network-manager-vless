# Architecture

## Components

| Component | Runs as | Source | Responsibility |
|---|---|---|---|
| `nm-vless-service` | root, one process per active connection, started by NetworkManager | `src/nm_vless/service.py` | implements `org.freedesktop.NetworkManager.VPN.Plugin`, creates the TUN device, supervises Xray |
| `xray` | user `nm-vless`, no capabilities | Xray-core (external) | VLESS client; reads and writes IP packets on the TUN device |
| `nm-vless` | the desktop user | `src/nm_vless/cli.py` | creates profiles from links and subscriptions through libnm |
| `nm-vless-subscriptions.timer` | the desktop user (systemd `--user`) | `data/` | periodic `nm-vless subscription update` |
| `nm-vless-proxy-guard.service` | the desktop user (systemd `--user`) | `src/nm_vless/proxyguard.py` | turns another client's local system proxy off while VLESS is connected |
| `libnm-vpn-plugin-vless.so` | inside GNOME Settings / nmcli | `src/editor/` | `NMVpnEditorPlugin`: plugin name, import of link files, loads the editor |
| `libnm-gtk4-vpn-plugin-vless-editor.so` | inside GNOME Settings | `src/editor/` | GTK 4 settings page |

The pure modules `link.py` (share links), `settings.py` (profile keys) and `xray.py`
(Xray configuration) have no GObject dependencies and are shared by the service and the
tool. All values are validated when a `VlessServer` is constructed, so the privileged
service never acts on unvalidated input.

## GNOME Settings editor

GNOME Settings finds the plugin through the `[libnm]` section of
`nm-vless-service.name` and loads `libnm-vpn-plugin-vless.so`, which loads the GTK 4
editor from the same directory on demand (like the OpenVPN plugin, so the base plugin
also works in processes without GTK). The editor writes only the keys that apply to the
selected transport and security, keeps keys it does not know, and never parses links
itself: "Fill In" and "Import from file" run `nm-vless parse-link`, so the Python
parser and its tests are the only implementation.

## Connection sequence

```text
NetworkManager                 nm-vless-service (root)                    xray (nm-vless)
      │  NeedSecrets ────────────────►│  answered by libnm: none needed (see below)
      │  Connect(settings) ──────────►│  validate vpn.data / vpn.secrets
      │                               │  check xray binary (owner, mode, version)
      │◄──────────────── return ──────│
      │                               │  resolve server (Gio.Resolver, async)
      │                               │  open /dev/net/tun → fd, "vless0"
      │                               │  spawn: setpriv --reuid=nm-vless … xray run
      │                               │         -config stdin:   (fd 3 = TUN)
      │                               │────────── JSON config on stdin ─────────►│
      │                               │◄──────── "Xray 26.x started" ────────────│
      │◄──── Config (tundev, mtu, ext. gateway = server IP)
      │◄──── Ip4Config / Ip6Config (address, DNS, default route)
      │  configure vless0, routes, DNS; route server IP via the physical device
      ⋮
      │  Disconnect ─────────────────►│  SIGTERM xray (SIGKILL after 3 s), close TUN fd
```

`NeedSecrets` is answered by libnm itself ("no secrets needed"): the introspection data
of `NMVpnServicePluginClass.need_secrets` declares its out-parameter as an input, so it
cannot be overridden from Python safely. NetworkManager loads system-owned secrets before
calling `Connect`, and `Connect` rejects a profile without `uuid` with `BadArguments`.

If Xray exits before the connection is configured, fails to report readiness within
20 seconds or the server cannot be resolved, the service sends
`Failure(CONNECT_FAILED)` and stops; the last Xray output line is logged. If Xray exits
while connected, it is restarted after 2 seconds on the same TUN device with the same
configuration, which NetworkManager does not notice; more than 3 exits within 5
minutes are treated as a failure.

## Secret agents

GNOME Shell (and other secret agents) run the helper named by `[GNOME] auth-dialog`
whenever NetworkManager asks them for VPN secrets, for example when GNOME Settings
opens a connection. GNOME Shell never answers such a request if the plugin has no
helper, so each one waited for NetworkManager's 25-second timeout.
`nm-vless-auth-dialog` reads the request and answers at once: nothing to ask when the
user id is stored by NetworkManager (flags 0, the default), otherwise an
external-UI description that makes GNOME Shell show its password dialog.

## Routing without loops

A full tunnel routes everything through `vless0`, including the traffic Xray itself
sends to the server. Loops are avoided without firewall marks or policy routing:

1. The service resolves the server name before the tunnel exists and writes the
   resulting IP address into the Xray outbound. The original domain is kept for TLS
   SNI and the HTTP `Host` header, so CDNs and REALITY keep working.
2. The same address is reported to NetworkManager as the VPN's external gateway
   (`gateway` in the `Config` signal). NetworkManager adds a host route for it via the
   parent device (see `auto-route-ext-gw` in `nm-settings(5)`).
3. Xray only ever connects to that one address, so its traffic always takes the host
   route.

IPv4 is preferred when the server has both address families.

## Addresses and DNS

- Tunnel addresses: `198.18.0.1/30` (RFC 2544 benchmark range, avoids clashes with home,
  Docker and CGNAT networks) and `fd6e:6d76:6c65::1/64` (RFC 4193 ULA). Xray's user-space
  network stack accepts packets for any destination, so the addresses are only local.
- DNS servers from the profile (default Cloudflare) are pushed to NetworkManager, which
  configures them on `vless0` in systemd-resolved. Queries to them are routed through
  the tunnel like any other traffic.
- Profiles use `ipv4.dns-priority` and `ipv6.dns-priority` −50. A negative priority
  makes NetworkManager use only the tunnel's DNS servers while it is up (it gives the
  tunnel the `~.` routing domain in systemd-resolved), so no query goes to the DNS
  server of the physical network.
- With IPv6 enabled (default), an IPv6 default route also points into the tunnel, so
  IPv6 traffic cannot bypass the VPN. If the server has no IPv6 connectivity, IPv6
  connections fail quickly and applications fall back to IPv4.
- Xray sniffs the domain of each connection (TLS SNI, HTTP `Host`, QUIC) and sends it
  to the server instead of the IP address, like other Xray clients. The server then
  resolves the name itself and can apply its domain-based routing; servers that route
  some sites through another exit drop connections that arrive with only an IP address.

## Privilege separation

NetworkManager starts VPN services as root. The service needs root for exactly one
operation, creating the TUN device, and hands the file descriptor to Xray through
`XRAY_TUN_FD` (supported by Xray-core since v26.6.22). Xray then runs with:

- `setpriv --reuid=nm-vless --regid=nm-vless --clear-groups`
- `--no-new-privs --inh-caps=-all --bounding-set=-all`
- an environment containing only `PATH` and `XRAY_TUN_FD`, working directory `/`.

Xray does not need `CAP_NET_ADMIN`: NetworkManager configures addresses, routes and the
MTU of the device.

## Handling of secrets

- The VLESS user id is a NetworkManager secret (`vpn.secrets.uuid`, flag 0), stored in
  `/etc/NetworkManager/system-connections/` with mode 0600. `vpn.data` never contains it.
- Xray receives its configuration on stdin; it is never written to disk.
- `VlessServer.__repr__` hides the id and error messages never include it.
- Subscription URLs contain access tokens: they live in
  `~/.config/nm-vless/subscriptions.json` (mode 0600) and are not printed unless
  `--show-urls` is given. Errors mention only the host name.
- The service refuses Xray binaries that are not owned by root or are writable by
  other users anywhere on their path.

## Subscriptions

`nm-vless subscription update` downloads the URL (HTTPS only, certificate verification,
no redirects to HTTP, 4 MiB limit), decodes base64 when needed and parses every line.
Profiles are linked to a subscription through two `vpn.data` keys, which the editor
keeps and the service ignores:

| Key | Value |
|---|---|
| `subscription` | subscription id |
| `server-key` | hash of address, port, transport, path and gRPC service name |

Before 0.5.0 these were `NMSettingUser` keys. Ubuntu stores profiles through netplan,
which turns the `user` setting into an invalid keyfile group ("invalid setting name
'user.nm-vless'") and loses it, so every update added the servers again. When no
profile carries a subscription's mark, an update adopts unmarked profiles whose name
and server key match one of its servers (the active or most recently used copy) and
deletes the other copies unless they are active. Once marked profiles exist, unmarked
ones are treated as the user's own.

The server key excludes the user id and the name, so credential rotation or renaming
updates the existing profile. Updates keep the profile UUID, permissions, autoconnect,
tunnel options and IP settings. Profiles that are active are never deleted, and an
update without any usable server is rejected instead of deleting everything.

Subscriptions can be added from the GNOME Settings editor: an `https://` URL in "Import
from link" runs `nm-vless subscription add - --keyfile` as the desktop user, which
creates the profiles through NetworkManager and starts the update timer. The editor only
shows the summary; it never sees the servers' credentials.

The user timer `nm-vless-subscriptions.timer` runs hourly and calls
`nm-vless subscription update --due`. A subscription is due when its interval has
passed since the last attempt; the interval comes from the provider's
`profile-update-interval` (clamped to 1 hour – 1 week) or defaults to 6 hours. Failed
attempts also count, so an unreachable provider is not polled every hour.

## Traffic that bypasses the tunnel

The tunnel only receives traffic that the kernel routes to it, and applications only
send it there if they do not talk to a proxy.

Routes: besides the default route, the service reports `0.0.0.0/1` and `128.0.0.0/1`
(`::/1` and `8000::/1`) on the TUN device. Longer prefixes win regardless of metrics,
so a default route of another client's TUN device (Happ's `happ-xray` uses metric 1,
below the VPN's 50) no longer takes the traffic, while the host route to the server
and LAN or container routes stay more specific. The halves are left out for a family
whose profile sets `never-default`, like the default route itself. `nm-vless check`
(and the service, in the journal, when connecting) reports foreign TUN devices whose
routes still win: half-default routes with a better metric, or any route of a family
the VLESS tunnel does not carry.

System proxy: Happ, v2rayN and similar clients set the GNOME proxy to their local
proxy, so browsers and Electron applications bypass any routing. The user service
`nm-vless-proxy-guard.service` (`nm-vless proxy-guard`) watches NetworkManager and
`org.gnome.system.proxy`. While a VLESS connection is activated and the proxy mode is
`manual` with a loopback host, it saves the mode in
`$XDG_STATE_HOME/nm-vless/system-proxy-mode`, sets it to `none` and shows a desktop
notification; if the other client sets the proxy again, it is turned off again. When
no VLESS connection is active (or the guard stops), the saved mode is restored if the
mode is still `none`, otherwise the user's choice is kept. Remote proxies (a company
proxy) are never touched. Applications that copied the proxy into environment
variables of their child processes (`HTTPS_PROXY`) keep it until they are restarted;
`nm-vless check` reports such variables in its own session.
