# SPDX-License-Identifier: GPL-2.0-or-later
"""Parse and format ``vless://`` share links.

The format is the de-facto standard shared by Xray-based clients, see
https://github.com/XTLS/Xray-core/discussions/716.

Every :class:`VlessServer` is validated on construction, so code that receives
one (in particular the privileged VPN service) can rely on its invariants.
Error messages never contain the user id, which is a credential.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import re
import unicodedata
import uuid as uuidlib
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

__all__ = [
    "FLOWS",
    "GRPC_MODES",
    "NETWORKS",
    "SECURITIES",
    "XHTTP_MODES",
    "LinkError",
    "VlessServer",
    "format_link",
    "is_ip_address",
    "parse_link",
]

NETWORKS: Final = ("tcp", "ws", "grpc", "xhttp", "httpupgrade")
SECURITIES: Final = ("none", "tls", "reality")
FLOWS: Final = ("", "xtls-rprx-vision")
XHTTP_MODES: Final = ("", "auto", "packet-up", "stream-up", "stream-one")
GRPC_MODES: Final = ("", "gun", "multi")

_NETWORK_ALIASES: Final = {"raw": "tcp", "splithttp": "xhttp"}
_PATH_NETWORKS: Final = frozenset({"ws", "xhttp", "httpupgrade"})
_TRUE_VALUES: Final = frozenset({"1", "true", "yes"})

_MAX_TEXT: Final = 1024
_MAX_EXTRA: Final = 16384
_MAX_ALPN: Final = 8
_MAX_CUSTOM_ID_BYTES: Final = 30  # Xray maps short strings to UUIDv5

_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9._/-]{1,64}$")
_REALITY_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SHORT_ID_RE: Final = re.compile(r"^(?:[0-9a-f]{2}){0,8}$")
_ENCRYPTION_RE: Final = re.compile(r"^[A-Za-z0-9._+/=-]{1,4096}$")
_LABEL: Final = r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
_HOSTNAME_RE: Final = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*\.?$")


class LinkError(ValueError):
    """A link or server description is malformed or uses unsupported features."""


def is_ip_address(value: str) -> bool:
    """Return True if *value* is a literal IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _short(value: str, limit: int = 32) -> str:
    """Shorten an untrusted value for use in an error message."""
    return value if len(value) <= limit else value[:limit] + "…"


def _check_text(name: str, value: str, *, max_len: int = _MAX_TEXT, spaces: bool = True) -> None:
    if len(value) > max_len:
        raise LinkError(f"{name} is too long")
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise LinkError(f"{name} contains control characters")
    if not spaces and any(ch.isspace() for ch in value):
        raise LinkError(f"{name} must not contain spaces")


def _check_hostname(name: str, value: str) -> None:
    if is_ip_address(value):
        return
    if len(value) > 253 or not _HOSTNAME_RE.fullmatch(value):  # noqa: PLR2004 - DNS limit
        raise LinkError(f"{name} is not a valid host name: {_short(value)!r}")


def _normalize_host(value: str) -> str:
    """Return a canonical form of a host name or IP address (IDNA, lower case)."""
    value = value.strip()
    if is_ip_address(value):
        return str(ipaddress.ip_address(value))
    try:
        return value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise LinkError(f"invalid host name: {_short(value)!r}") from exc


def _check_user_id(value: str) -> None:
    try:
        uuidlib.UUID(value)
    except ValueError:
        encoded = value.encode("utf-8")
        if not 1 <= len(encoded) <= _MAX_CUSTOM_ID_BYTES:
            raise LinkError("user id must be a UUID or a string of 1-30 bytes") from None
        _check_text("user id", value, spaces=False)


@dataclass(frozen=True, slots=True)
class VlessServer:
    """A validated description of one VLESS server.

    Attribute names follow Xray-core terminology. Transport-specific fields that
    do not apply to the chosen ``network`` or ``security`` must be empty.
    """

    name: str
    address: str
    port: int
    uuid: str = field(repr=False)
    encryption: str = "none"
    flow: str = ""
    network: str = "tcp"
    security: str = "none"
    sni: str = ""
    fingerprint: str = ""
    alpn: tuple[str, ...] = ()
    public_key: str = ""
    short_id: str = ""
    spider_x: str = ""
    path: str = ""
    host: str = ""
    service_name: str = ""
    mode: str = ""
    extra: str = ""

    def __post_init__(self) -> None:
        self._validate_endpoint()
        self._validate_security()
        self._validate_transport()

    @property
    def endpoint(self) -> str:
        """``host:port`` with IPv6 addresses in brackets."""
        host = f"[{self.address}]" if ":" in self.address else self.address
        return f"{host}:{self.port}"

    def _validate_endpoint(self) -> None:
        if not self.name.strip():
            raise LinkError("name must not be empty")
        _check_text("name", self.name, max_len=256)
        _check_hostname("address", self.address)
        if isinstance(self.port, bool) or not 1 <= self.port <= 65535:  # noqa: PLR2004
            raise LinkError("port must be in range 1-65535")
        _check_user_id(self.uuid)
        if not _ENCRYPTION_RE.fullmatch(self.encryption):
            raise LinkError("invalid encryption value")

    def _validate_security(self) -> None:
        if self.security not in SECURITIES:
            raise LinkError(f"unsupported security {_short(self.security)!r}")
        if self.flow not in FLOWS:
            raise LinkError(f"unsupported flow {_short(self.flow)!r}")
        if self.flow and (self.network != "tcp" or self.security == "none"):
            raise LinkError(f"flow {self.flow!r} requires tcp transport with tls or reality")

        if self.security == "none":
            if self.sni or self.fingerprint or self.alpn:
                raise LinkError("sni, fingerprint and alpn require tls or reality")
        else:
            if self.sni:
                _check_text("sni", self.sni, max_len=253, spaces=False)
            if self.fingerprint and not _TOKEN_RE.fullmatch(self.fingerprint):
                raise LinkError(f"invalid fingerprint {_short(self.fingerprint)!r}")

        if len(self.alpn) > _MAX_ALPN or any(not _TOKEN_RE.fullmatch(a) for a in self.alpn):
            raise LinkError("invalid alpn list")
        if self.alpn and self.security != "tls":
            raise LinkError("alpn is only supported with tls")

        if self.security == "reality":
            if not self.sni:
                raise LinkError("reality requires sni")
            if not _REALITY_KEY_RE.fullmatch(self.public_key):
                raise LinkError("reality requires a valid public key (pbk)")
            if not _SHORT_ID_RE.fullmatch(self.short_id):
                raise LinkError("reality short id (sid) must be up to 16 hex digits")
            _check_text("spider x", self.spider_x, spaces=False)
        elif self.public_key or self.short_id or self.spider_x:
            raise LinkError("pbk, sid and spx are only supported with reality")

    def _validate_transport(self) -> None:
        if self.network not in NETWORKS:
            raise LinkError(f"unsupported transport {_short(self.network)!r}")

        if self.network in _PATH_NETWORKS:
            _check_text("path", self.path, spaces=False)
            if self.host:
                _check_hostname("host", self.host)
        elif self.path or self.host:
            raise LinkError(f"path and host are not supported with {self.network}")

        if self.network == "grpc":
            _check_text("service name", self.service_name, max_len=256, spaces=False)
        elif self.service_name:
            raise LinkError("service name is only supported with grpc")

        allowed_modes = {"xhttp": XHTTP_MODES, "grpc": GRPC_MODES}.get(self.network, ("",))
        if self.mode not in allowed_modes:
            raise LinkError(f"unsupported mode {_short(self.mode)!r} for {self.network}")

        if self.extra:
            if self.network != "xhttp":
                raise LinkError("extra is only supported with xhttp")
            if len(self.extra) > _MAX_EXTRA:
                raise LinkError("extra is too long")
            try:
                extra = json.loads(self.extra)
            except json.JSONDecodeError as exc:
                raise LinkError("extra is not valid JSON") from exc
            if not isinstance(extra, dict):
                raise LinkError("extra must be a JSON object")


def _first_values(query: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for key, value in parse_qsl(query, keep_blank_values=True):
        params.setdefault(key, value.strip())
    return params


def parse_link(link: str) -> VlessServer:
    """Parse a ``vless://`` link into a validated :class:`VlessServer`.

    Parameters that do not apply to the selected transport are ignored, as other
    clients do. Features that would silently change security (for example
    ``allowInsecure``) are rejected instead.
    """
    try:
        parts = urlsplit(link.strip())
        port = parts.port
    except ValueError as exc:
        raise LinkError("malformed link") from exc

    if parts.scheme.lower() != "vless":
        raise LinkError("not a vless:// link")
    if not parts.username:
        raise LinkError("missing user id")
    if parts.password is not None:
        raise LinkError("unexpected password in link")
    if not parts.hostname:
        raise LinkError("missing server address")
    if port is None:
        raise LinkError("missing port")

    user_id = unquote(parts.username)
    with contextlib.suppress(ValueError):  # custom ids are validated by VlessServer
        user_id = str(uuidlib.UUID(user_id))

    params = _first_values(parts.query)
    network = params.get("type", "").lower() or "tcp"
    network = _NETWORK_ALIASES.get(network, network)
    security = params.get("security", "").lower() or "none"

    if params.get("allowInsecure", "").lower() in _TRUE_VALUES or (
        params.get("insecure", "").lower() in _TRUE_VALUES
    ):
        raise LinkError("insecure TLS (allowInsecure) is not supported")
    if network == "tcp" and params.get("headerType", "none").lower() not in ("", "none"):
        raise LinkError("tcp header obfuscation (headerType) is not supported")

    address = _normalize_host(parts.hostname)
    fields: dict[str, object] = {
        "name": unquote(parts.fragment).strip() or f"{address}:{port}",
        "address": address,
        "port": port,
        "uuid": user_id,
        "encryption": params.get("encryption", "") or "none",
        "flow": params.get("flow", "") if network == "tcp" else "",
        "network": network,
        "security": security,
    }

    if security in ("tls", "reality"):
        sni = params.get("sni", "") or params.get("peer", "")
        fields["sni"] = _normalize_host(sni) if sni else ""
        fields["fingerprint"] = params.get("fp", "") or ("chrome" if security == "reality" else "")
    if security == "tls":
        fields["alpn"] = tuple(a.strip() for a in params.get("alpn", "").split(",") if a.strip())
    if security == "reality":
        fields["public_key"] = params.get("pbk", "")
        fields["short_id"] = params.get("sid", "").lower()
        fields["spider_x"] = params.get("spx", "")

    if network in _PATH_NETWORKS:
        fields["path"] = params.get("path", "")
        host = params.get("host", "")
        fields["host"] = _normalize_host(host) if host else ""
    if network == "grpc":
        fields["service_name"] = params.get("serviceName", "")
    if network in ("xhttp", "grpc"):
        fields["mode"] = params.get("mode", "").lower()
    if network == "xhttp" and params.get("extra"):
        try:
            extra = json.loads(params["extra"])
        except json.JSONDecodeError as exc:
            raise LinkError("extra is not valid JSON") from exc
        fields["extra"] = json.dumps(extra, separators=(",", ":"), sort_keys=True)

    return VlessServer(**fields)  # type: ignore[arg-type]


def format_link(server: VlessServer) -> str:
    """Format *server* as a ``vless://`` link; ``parse_link`` restores it exactly."""
    params: list[tuple[str, str]] = [
        ("encryption", server.encryption),
        ("type", server.network),
        ("security", server.security),
    ]
    optional = [
        ("flow", server.flow),
        ("sni", server.sni),
        ("fp", server.fingerprint),
        ("alpn", ",".join(server.alpn)),
        ("pbk", server.public_key),
        ("sid", server.short_id),
        ("spx", server.spider_x),
        ("path", server.path),
        ("host", server.host),
        ("serviceName", server.service_name),
        ("mode", server.mode),
        ("extra", server.extra),
    ]
    params.extend((key, value) for key, value in optional if value)
    query = urlencode(params, quote_via=quote)
    return f"vless://{quote(server.uuid, safe='')}@{server.endpoint}?{query}#{quote(server.name)}"
