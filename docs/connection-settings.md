# Connection settings

A VLESS profile is a NetworkManager connection of type `vpn` with
`vpn.service-type = org.freedesktop.NetworkManager.vless`. The keys below are the
plugin's stable interface; they can be edited with `nmcli connection modify` or in
`/etc/NetworkManager/system-connections/*.nmconnection`.

Unknown keys are ignored with a warning in the log. Invalid values make the connection
fail with an error that names the key.

## Secrets (`vpn.secrets`)

| Key | Description |
|---|---|
| `uuid` | VLESS user id: a UUID or a string of 1–30 bytes. Stored by NetworkManager (`uuid-flags=0`). |

## Server (`vpn.data`)

| Key | Link parameter | Values | Default |
|---|---|---|---|
| `address` | host | host name or IP address (required) | — |
| `port` | port | 1–65535 (required) | — |
| `encryption` | `encryption` | `none` or an Xray VLESS encryption string | `none` |
| `flow` | `flow` | `xtls-rprx-vision` (tcp with tls or reality only) | empty |
| `network` | `type` | `tcp`, `ws`, `grpc`, `xhttp`, `httpupgrade` | `tcp` |
| `security` | `security` | `none`, `tls`, `reality` | `none` |
| `sni` | `sni` | server name for TLS/REALITY; required for REALITY | server domain |
| `fingerprint` | `fp` | uTLS fingerprint, e.g. `chrome`, `firefox` | empty (REALITY: `chrome`) |
| `alpn` | `alpn` | comma-separated list, TLS only | empty |
| `reality-public-key` | `pbk` | 43-character base64url key | — |
| `reality-short-id` | `sid` | up to 16 hex digits | empty |
| `reality-spider-x` | `spx` | path | empty |
| `path` | `path` | ws, httpupgrade, xhttp | `/` |
| `host` | `host` | HTTP host for ws, httpupgrade, xhttp | server domain |
| `grpc-service-name` | `serviceName` | gRPC service name | empty |
| `mode` | `mode` | xhttp: `auto`, `packet-up`, `stream-up`, `stream-one`; grpc: `gun`, `multi` | `auto` / `gun` |
| `xhttp-extra` | `extra` | JSON object with advanced XHTTP settings | empty |

Keys that do not apply to the chosen `network` or `security` must be absent.

## Tunnel (`vpn.data`)

| Key | Values | Default |
|---|---|---|
| `mtu` | 1280–9000 (576–9000 with `ipv6=no`) | `1500` |
| `dns` | 1–4 IPv4 addresses, comma-separated | `1.1.1.1,1.0.0.1` |
| `dns6` | 1–4 IPv6 addresses, comma-separated | `2606:4700:4700::1111,2606:4700:4700::1001` |
| `ipv6` | `yes`: route IPv6 through the tunnel; `no`: leave IPv6 untouched | `yes` |

## Subscription (`vpn.data`)

Set by `nm-vless` for profiles created from subscriptions and ignored by the
service; see [architecture.md](architecture.md#subscriptions).

| Key | Description |
|---|---|
| `subscription` | id of the subscription the profile belongs to |
| `server-key` | identity of the server within the subscription |

Versions before 0.5.0 stored these as `nm-vless.subscription` and
`nm-vless.server-key` in the `user` setting. Ubuntu's netplan integration drops
that setting, so subscription updates could not find their profiles and created
new ones; the next update moves the keys and removes the duplicates.

## Example

```sh
nmcli connection add type vpn con-name "Example" \
    vpn-type org.freedesktop.NetworkManager.vless \
    vpn.data "address=vpn.example.com, port=443, network=xhttp, security=tls, path=/xh, uuid-flags=0" \
    vpn.secrets "uuid=b831381d-6324-4d53-ad4f-8cda48b30811"
```

Importing a link with `nm-vless import -` is easier and validates every value first.
