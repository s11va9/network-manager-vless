# SPDX-License-Identifier: GPL-2.0-or-later
"""Synchronise the servers of a subscription with NetworkManager profiles."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from nm_vless import settings
from nm_vless.link import VlessServer
from nm_vless.nmclient import (
    build_connection,
    data_server_key,
    has_legacy_user_data,
    owned_by_subscription,
    server_key,
    server_key_of,
    subscription_items,
    subscription_of,
    vpn_data,
)

__all__ = ["ConnectionStore", "SyncReport", "sync_subscription"]


class ConnectionStore(Protocol):
    """The subset of :class:`nm_vless.nmclient.NMSession` used for synchronisation."""

    def vless_connections(self) -> list[Any]: ...
    def active_uuids(self) -> set[str]: ...
    def add(self, connection: Any) -> Any: ...
    def update(self, remote: Any, connection: Any) -> None: ...
    def delete(self, remote: Any) -> None: ...
    def secrets(self, remote: Any) -> dict[str, str]: ...


@dataclass(slots=True)
class SyncReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept_active: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{len(self.added)} added",
            f"{len(self.updated)} updated",
            f"{len(self.unchanged)} unchanged",
            f"{len(self.removed)} removed",
        ]
        if self.kept_active:
            parts.append(f"{len(self.kept_active)} kept because active")
        return ", ".join(parts)


def _unique_keys(servers: Sequence[VlessServer]) -> list[tuple[str, VlessServer]]:
    seen: dict[str, int] = {}
    result: list[tuple[str, VlessServer]] = []
    for server in servers:
        base = server_key(server)
        count = seen.get(base, 0)
        seen[base] = count + 1
        result.append((base if count == 0 else f"{base}-{count}", server))
    return result


def _is_current(
    store: ConnectionStore, remote: Any, server: VlessServer, subscription_id: str, key: str
) -> bool:
    data, secrets = settings.server_to_items(server)
    data.update(subscription_items(subscription_id, key))
    current = {k: v for k, v in vpn_data(remote).items() if k not in settings.TUNNEL_KEYS}
    if remote.get_id() != server.name or current != data or has_legacy_user_data(remote):
        return False
    return store.secrets(remote).get(settings.SECRET_UUID) == secrets[settings.SECRET_UUID]


def _copies(unmarked: list[Any], server: VlessServer) -> list[Any]:
    """Profiles without a subscription that were made from *server*."""
    key = server_key(server)
    return [
        remote
        for remote in unmarked
        if remote.get_id() == server.name and data_server_key(vpn_data(remote)) == key
    ]


def _adopt(unmarked: list[Any], server: VlessServer, active: set[str]) -> Any | None:
    """Remove and return the best copy of *server* from *unmarked*.

    The active copy is preferred, then the most recently used one.
    """
    copies = _copies(unmarked, server)
    if not copies:
        return None
    best = max(
        copies,
        key=lambda r: (r.get_uuid() in active, r.get_setting_connection().get_timestamp()),
    )
    unmarked.remove(best)
    return best


def sync_subscription(
    store: ConnectionStore,
    subscription_id: str,
    servers: Sequence[VlessServer],
    *,
    owner: str | None,
) -> SyncReport:
    """Make the profiles of *subscription_id* match *servers*.

    Profiles are matched by server key, so user changes to autoconnect, tunnel
    options and IP settings survive updates. Active profiles are never deleted.

    If no profile carries the subscription's mark, profiles without any mark that
    have the name and server of an entry are adopted, and further such copies are
    deleted: older versions stored the mark where netplan loses it, and created the
    servers again on every update (see docs/architecture.md). Once marked profiles
    exist, unmarked ones are the user's own and left alone.

    Raises:
        ValueError: *servers* is empty. An empty response usually means a
            provider problem, and deleting every profile would be destructive.
    """
    if not servers:
        raise ValueError("subscription contains no supported servers; nothing was changed")

    report = SyncReport()
    connections = store.vless_connections()
    active = store.active_uuids()
    stale: list[Any] = []
    existing: dict[str | None, Any] = {}
    for remote in owned_by_subscription(connections, subscription_id):
        key = server_key_of(remote)
        if key in existing:
            stale.append(remote)
        else:
            existing[key] = remote
    unmarked = (
        [remote for remote in connections if subscription_of(remote) is None]
        if not existing
        else []
    )
    entries = _unique_keys(servers)
    for key, server in entries:
        remote = existing.pop(key, None) or _adopt(unmarked, server, active)
        if remote is None:
            store.add(
                build_connection(server, owner=owner, subscription_id=subscription_id, key=key)
            )
            report.added.append(server.name)
        elif _is_current(store, remote, server, subscription_id, key):
            report.unchanged.append(server.name)
        else:
            store.update(
                remote,
                build_connection(
                    server, owner=owner, subscription_id=subscription_id, key=key, base=remote
                ),
            )
            report.updated.append(server.name)

    stale.extend(existing.values())
    for _key, server in entries:
        stale.extend(r for r in _copies(unmarked, server) if r not in stale)
    for remote in stale:
        if remote.get_uuid() in active:
            report.kept_active.append(remote.get_id())
            continue
        store.delete(remote)
        report.removed.append(remote.get_id())
    return report
