# SPDX-License-Identifier: GPL-2.0-or-later
"""Signal registration must work on both Debian's older GLib and newer Fedora."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from nm_vless import glibcompat


@pytest.mark.parametrize("modern", [True, False])
def test_signal_api_selection(monkeypatch: pytest.MonkeyPatch, modern: bool) -> None:
    calls: list[tuple[int, int, Callable[[], bool]]] = []

    def register(priority: int, signum: int, callback: Callable[[], bool]) -> int:
        calls.append((priority, signum, callback))
        return 42

    def unexpected(*_args: object) -> None:
        pytest.fail("wrong signal namespace")

    monkeypatch.setattr(
        glibcompat, "GLibUnix", SimpleNamespace(signal_add=register) if modern else None
    )
    monkeypatch.setattr(
        glibcompat, "GLib", SimpleNamespace(unix_signal_add=unexpected if modern else register)
    )

    def callback() -> bool:
        return False

    assert glibcompat.unix_signal_add(-100, 15, callback) == 42
    assert calls == [(-100, 15, callback)]
