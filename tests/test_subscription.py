# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import base64
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nm_vless.subscription import (
    Subscription,
    SubscriptionError,
    SubscriptionStore,
    TrafficInfo,
    decode_payload,
    parse_subscription,
    validate_url,
)

from .conftest import REALITY_LINK, TEST_UUID, WS_LINK, XHTTP_LINK

PLAIN = "\n".join([REALITY_LINK, WS_LINK, "", XHTTP_LINK]).encode()


def test_plain_text() -> None:
    assert decode_payload(PLAIN) == PLAIN.decode()


@pytest.mark.parametrize(
    "encode",
    [
        base64.b64encode,
        base64.urlsafe_b64encode,
        lambda data: base64.b64encode(data).rstrip(b"="),
        lambda data: b"\n".join(base64.encodebytes(data).splitlines()),
    ],
)
def test_base64_variants(encode: object) -> None:
    assert callable(encode)
    assert decode_payload(encode(PLAIN)) == PLAIN.decode()


def test_utf8_bom() -> None:
    assert decode_payload(b"\xef\xbb\xbf" + PLAIN) == PLAIN.decode()


def test_unrecognized_payload() -> None:
    with pytest.raises(SubscriptionError, match="unrecognized"):
        decode_payload(b"<html>Not found</html>")


def test_parse_collects_errors_and_unsupported() -> None:
    body = "\n".join(
        [
            "#profile-title: base64:" + base64.b64encode("Мой VPN".encode()).decode(),
            "#subscription-userinfo: upload=10; download=20; total=100; expire=1893456000",
            REALITY_LINK,
            "vmess://eyJ2IjoiMiJ9",
            "trojan://pw@example.com:443",
            f"vless://{TEST_UUID}@example.com:443?type=kcp#bad",
            "garbage",
        ]
    ).encode()
    parsed = parse_subscription(body)
    assert [s.name for s in parsed.servers] == ["Reality 🇳🇱"]
    assert parsed.title == "Мой VPN"
    assert parsed.traffic == TrafficInfo(upload=10, download=20, total=100, expire=1893456000)
    assert dict(parsed.unsupported) == {"vmess": 1, "trojan": 1}
    assert parsed.errors == ["line 6: unsupported transport 'kcp'", "line 7: not a link"]
    assert not any(TEST_UUID in error for error in parsed.errors)


def test_headers_take_precedence() -> None:
    parsed = parse_subscription(
        b"#profile-title: body title\n" + REALITY_LINK.encode(),
        {"Profile-Title": "header title", "subscription-userinfo": "total=5"},
    )
    assert parsed.title == "header title"
    assert parsed.traffic.total == 5


@pytest.mark.parametrize(
    "url", ["http://example.com/sub", "ftp://example.com", "example.com/sub", "https://"]
)
def test_only_https_urls(url: str) -> None:
    with pytest.raises(SubscriptionError, match="https://"):
        validate_url(url)


def test_store_round_trip_is_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    store = SubscriptionStore()
    assert store.load() == []
    sub = Subscription(id="abcd1234", url="https://example.com/s/token", name="Test")
    sub.mark_updated(
        parse_subscription(REALITY_LINK.encode(), {"subscription-userinfo": "total=1"})
    )
    store.save([sub])

    assert store.path == tmp_path / "nm-vless" / "subscriptions.json"
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert store.load() == [sub]
    assert "token" not in repr(sub)
    assert store.get([sub], "Test") is sub
    with pytest.raises(SubscriptionError):
        store.get([sub], "missing")


def test_store_rejects_unknown_format(tmp_path: Path) -> None:
    path = tmp_path / "subs.json"
    path.write_text('{"version": 99}')
    with pytest.raises(SubscriptionError, match="unsupported file format"):
        SubscriptionStore(path).load()


@pytest.mark.parametrize(
    ("value", "expected"), [("12", 12), (" 24 ", 24), ("0", 1), ("100000", 168), ("x", None)]
)
def test_update_interval_from_provider(value: str, expected: int | None) -> None:
    parsed = parse_subscription(REALITY_LINK.encode(), {"profile-update-interval": value})
    assert parsed.update_interval_hours == expected


def test_update_interval_from_body_metadata() -> None:
    body = b"#profile-update-interval: 3\n" + REALITY_LINK.encode()
    assert parse_subscription(body).update_interval_hours == 3


def test_is_due() -> None:
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    sub = Subscription(id="a", url="https://example.com/s", name="A")
    assert sub.is_due(now)  # never updated

    sub.mark_updated(
        parse_subscription(REALITY_LINK.encode(), {"profile-update-interval": "12"}),
        now=now - timedelta(hours=11),
    )
    assert sub.update_interval_hours == 12
    assert not sub.is_due(now)
    assert sub.is_due(now + timedelta(hours=1))


def test_failed_attempts_wait_for_the_interval() -> None:
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    sub = Subscription(id="a", url="https://example.com/s", name="A")
    sub.mark_attempted(now - timedelta(hours=1))
    assert not sub.is_due(now)
    assert sub.is_due(now + timedelta(hours=5))


def test_store_reads_files_from_older_versions(tmp_path: Path) -> None:
    path = tmp_path / "subs.json"
    path.write_text(
        '{"version": 1, "subscriptions": [{"id": "a", "url": "https://e.com/s", "name": "A",'
        ' "user_agent": "", "last_update": "", "last_error": "", "traffic": {}}]}'
    )
    (sub,) = SubscriptionStore(path).load()
    assert sub.update_interval_hours == 6
    assert sub.is_due()
