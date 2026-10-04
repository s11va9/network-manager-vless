/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Shared definitions of the NetworkManager-vless editor plugins.
 */
#pragma once

#include <NetworkManager.h>

G_BEGIN_DECLS

#define NM_VLESS_SERVICE_TYPE "org.freedesktop.NetworkManager.vless"

/*
 * Keys of vpn.data and vpn.secrets. They are the plugin's public interface and
 * must match src/nm_vless/settings.py; tests/test_editor.py checks this.
 */
#define NM_VLESS_KEY_ADDRESS            "address"
#define NM_VLESS_KEY_PORT               "port"
#define NM_VLESS_KEY_ENCRYPTION         "encryption"
#define NM_VLESS_KEY_FLOW               "flow"
#define NM_VLESS_KEY_NETWORK            "network"
#define NM_VLESS_KEY_SECURITY           "security"
#define NM_VLESS_KEY_SNI                "sni"
#define NM_VLESS_KEY_FINGERPRINT        "fingerprint"
#define NM_VLESS_KEY_ALPN               "alpn"
#define NM_VLESS_KEY_REALITY_PUBLIC_KEY "reality-public-key"
#define NM_VLESS_KEY_REALITY_SHORT_ID   "reality-short-id"
#define NM_VLESS_KEY_REALITY_SPIDER_X   "reality-spider-x"
#define NM_VLESS_KEY_PATH               "path"
#define NM_VLESS_KEY_HOST               "host"
#define NM_VLESS_KEY_GRPC_SERVICE_NAME  "grpc-service-name"
#define NM_VLESS_KEY_MODE               "mode"
#define NM_VLESS_KEY_XHTTP_EXTRA        "xhttp-extra"
#define NM_VLESS_KEY_MTU                "mtu"
#define NM_VLESS_KEY_DNS                "dns"
#define NM_VLESS_KEY_DNS6               "dns6"
#define NM_VLESS_KEY_IPV6               "ipv6"
#define NM_VLESS_KEY_UUID_FLAGS         "uuid-flags"
/* Written by "nm-vless subscription"; the editor keeps them unchanged. */
#define NM_VLESS_KEY_SUBSCRIPTION       "subscription"
#define NM_VLESS_KEY_SERVER_KEY         "server-key"

#define NM_VLESS_SECRET_UUID "uuid"

#define NM_VLESS_FLOW_VISION   "xtls-rprx-vision"
#define NM_VLESS_DEFAULT_PORT  443
#define NM_VLESS_DEFAULT_MTU   1500
/* Negative: only the tunnel's DNS servers are used while it is up (no DNS leaks).
 * Must match TUNNEL_DNS_PRIORITY in src/nm_vless/nmclient.py. */
#define NM_VLESS_DNS_PRIORITY  (-50)

/* Path of the nm-vless command; $NM_VLESS_CLI overrides it (used by the tests). */
const char *nm_vless_cli_path(void);

/* Run "nm-vless @args…" with @input on stdin and return its output; the last line of
 * stderr becomes the error message on failure. */
char *nm_vless_cli_sync(const char *const *args,
                        const char        *input,
                        GCancellable      *cancellable,
                        GError           **error);

void nm_vless_cli_async(const char *const  *args,
                        const char         *input,
                        GCancellable       *cancellable,
                        GAsyncReadyCallback callback,
                        gpointer            user_data);

char *nm_vless_cli_finish(GAsyncResult *result, GError **error);

/* Run "nm-vless parse-link" on @link and return its GKeyFile output. */
char *nm_vless_parse_link_sync(const char *link, GCancellable *cancellable, GError **error);

void nm_vless_parse_link_async(const char         *link,
                               GCancellable       *cancellable,
                               GAsyncReadyCallback callback,
                               gpointer            user_data);

char *nm_vless_parse_link_finish(GAsyncResult *result, GError **error);

/* Build a new VPN connection from the output of "nm-vless parse-link". */
NMConnection *nm_vless_connection_from_keyfile(const char *data, GError **error);

/* Return the first line of @text that starts with "vless://", or NULL. */
char *nm_vless_find_link(const char *text);

G_END_DECLS
