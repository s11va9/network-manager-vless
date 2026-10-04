# SPDX-License-Identifier: GPL-2.0-or-later
"""Fetching, decoding and storing subscriptions.

A subscription is an HTTPS URL returning share links, one per line, either as
plain text or base64-encoded. Common provider headers (``profile-title``,
``subscription-userinfo``) and ``#key: value`` lines in the body are understood.

Subscription URLs contain access tokens: they are stored with mode 0600 and
never included in error messages.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import ssl
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Any, Final
from urllib.parse import urlsplit

from nm_vless import __version__
from nm_vless.link import LinkError, VlessServer, parse_link

__all__ = [
    "DEFAULT_USER_AGENT",
    "FetchResult",
    "ParsedSubscription",
    "Subscription",
    "SubscriptionError",
    "SubscriptionStore",
    "TrafficInfo",
    "decode_payload",
    "fetch",
    "parse_subscription",
    "validate_url",
]

DEFAULT_USER_AGENT: Final = f"nm-vless/{__version__}"
#: Hours between automatic updates when the provider does not send profile-update-interval.
DEFAULT_UPDATE_HOURS: Final = 6
_MIN_UPDATE_HOURS: Final = 1
_MAX_UPDATE_HOURS: Final = 7 * 24
MAX_BODY_BYTES: Final = 4 * 1024 * 1024
_MAX_TITLE: Final = 128
_STORE_VERSION: Final = 1


class SubscriptionError(RuntimeError):
    """A subscription cannot be fetched, decoded or stored."""


def validate_url(url: str) -> str:
    """Return *url* stripped, or raise if it is not an ``https://`` URL."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise SubscriptionError("subscription URL must start with https://")
    return url


@dataclass(frozen=True, slots=True)
class FetchResult:
    body: bytes
    headers: Mapping[str, str]


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects that would downgrade to plain HTTP."""

    def redirect_request(  # noqa: PLR0917 - signature defined by urllib
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if urlsplit(newurl).scheme != "https":
            raise SubscriptionError("subscription redirected to a non-HTTPS URL")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, *, user_agent: str = DEFAULT_USER_AGENT, timeout: float = 30.0) -> FetchResult:
    """Download a subscription over HTTPS with certificate verification."""
    url = validate_url(url)
    host = urlsplit(url).hostname
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _HttpsOnlyRedirects(),
    )
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - validated https URL
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_BODY_BYTES + 1)
            response_headers = {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        raise SubscriptionError(f"{host} returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise SubscriptionError(f"cannot reach {host}: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise SubscriptionError(f"cannot reach {host}: {exc}") from None
    if len(body) > MAX_BODY_BYTES:
        raise SubscriptionError(f"subscription is larger than {MAX_BODY_BYTES // 1024} KiB")
    return FetchResult(body=body, headers=response_headers)


def decode_payload(body: bytes) -> str:
    """Return the subscription text, decoding base64 (standard or URL-safe) if needed."""
    try:
        text = body.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        raise SubscriptionError("subscription is not UTF-8 text") from None
    if "://" in text:
        return text
    compact = "".join(text.split())
    padded = compact + "=" * (-len(compact) % 4)
    for altchars in (None, b"-_"):
        try:
            return base64.b64decode(padded, altchars=altchars, validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            continue
    raise SubscriptionError("unrecognized subscription format")


def _clean_text(value: str) -> str:
    value = "".join(ch for ch in value if ch.isprintable()).strip()
    return value[:_MAX_TITLE]


def _decode_title(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("base64:"):
        encoded = "".join(value[7:].split())
        encoded += "=" * (-len(encoded) % 4)
        try:
            value = base64.b64decode(
                encoded, altchars=b"-_" if "-" in encoded or "_" in encoded else None
            ).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return ""
    return _clean_text(value)


@dataclass(frozen=True, slots=True)
class TrafficInfo:
    """Values of the ``subscription-userinfo`` header (bytes, Unix time)."""

    upload: int | None = None
    download: int | None = None
    total: int | None = None
    expire: int | None = None

    @classmethod
    def parse(cls, header: str) -> TrafficInfo:
        values: dict[str, int] = {}
        for item in header.split(";"):
            key, _, value = item.partition("=")
            key = key.strip().lower()
            if key in ("upload", "download", "total", "expire"):
                try:
                    values[key] = int(value.strip())
                except ValueError:
                    continue
        return cls(**values)


@dataclass(frozen=True, slots=True)
class ParsedSubscription:
    title: str
    servers: list[VlessServer]
    errors: list[str]
    unsupported: Counter[str]
    traffic: TrafficInfo
    #: Hours between updates requested by the provider, if any.
    update_interval_hours: int | None = None


def _parse_interval(value: str) -> int | None:
    """Parse ``profile-update-interval`` (hours), clamped to 1 hour - 1 week."""
    try:
        hours = int(float(value.strip()))
    except ValueError:
        return None
    return min(max(hours, _MIN_UPDATE_HOURS), _MAX_UPDATE_HOURS)


def parse_subscription(body: bytes, headers: Mapping[str, str] | None = None) -> ParsedSubscription:
    """Parse a subscription body; invalid lines are reported, not fatal."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    meta: dict[str, str] = {}
    servers: list[VlessServer] = []
    errors: list[str] = []
    unsupported: Counter[str] = Counter()

    for number, raw in enumerate(decode_payload(body).splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            key, sep, value = line[1:].partition(":")
            if sep:
                meta.setdefault(key.strip().lower(), value.strip())
            continue
        scheme, sep, _rest = line.partition("://")
        if not sep:
            errors.append(f"line {number}: not a link")
            continue
        if scheme.lower() != "vless":
            unsupported[scheme.lower()[:16]] += 1
            continue
        try:
            servers.append(parse_link(line))
        except LinkError as exc:
            errors.append(f"line {number}: {exc}")

    title = _decode_title(headers.get("profile-title") or meta.get("profile-title", ""))
    traffic = TrafficInfo.parse(
        headers.get("subscription-userinfo") or meta.get("subscription-userinfo", "")
    )
    interval = _parse_interval(
        headers.get("profile-update-interval") or meta.get("profile-update-interval", "")
    )
    return ParsedSubscription(title, servers, errors, unsupported, traffic, interval)


@dataclass(slots=True)
class Subscription:
    """A stored subscription."""

    id: str
    url: str = field(repr=False)
    name: str
    user_agent: str = ""
    last_update: str = ""
    last_error: str = ""
    traffic: dict[str, int | None] = field(default_factory=dict)
    update_interval_hours: int = DEFAULT_UPDATE_HOURS
    last_attempt: str = ""

    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(4)

    def mark_attempted(self, now: datetime | None = None) -> None:
        self.last_attempt = (now or datetime.now(UTC)).isoformat(timespec="seconds")

    def mark_updated(self, parsed: ParsedSubscription, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self.last_update = now.isoformat(timespec="seconds")
        self.last_attempt = self.last_update
        self.last_error = ""
        self.traffic = asdict(parsed.traffic)
        if parsed.update_interval_hours is not None:
            self.update_interval_hours = parsed.update_interval_hours

    def is_due(self, now: datetime | None = None) -> bool:
        """True if the update interval has passed since the last attempt.

        Failed attempts count too, so an unreachable provider is retried at the
        normal interval instead of on every timer run.
        """
        last = self.last_attempt or self.last_update
        if not last:
            return True
        try:
            previous = datetime.fromisoformat(last)
        except ValueError:
            return True
        return (now or datetime.now(UTC)) - previous >= timedelta(hours=self.update_interval_hours)


def _default_store_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "nm-vless" / "subscriptions.json"


class SubscriptionStore:
    """JSON file with the user's subscriptions, readable only by its owner."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_store_path()

    def load(self) -> list[Subscription]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise SubscriptionError(f"cannot read {self.path}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != _STORE_VERSION:
            raise SubscriptionError(f"{self.path}: unsupported file format")
        try:
            return [Subscription(**item) for item in raw.get("subscriptions", [])]
        except TypeError as exc:
            raise SubscriptionError(f"{self.path}: invalid entry") from exc

    def save(self, subscriptions: list[Subscription]) -> None:
        payload: dict[str, Any] = {
            "version": _STORE_VERSION,
            "subscriptions": [asdict(s) for s in subscriptions],
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".subscriptions-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:  # mkstemp uses mode 0600
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp).replace(self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def get(self, subscriptions: list[Subscription], sub_id: str) -> Subscription:
        for sub in subscriptions:
            if sub_id in (sub.id, sub.name):
                return sub
        raise SubscriptionError(f"no subscription {sub_id!r}")
