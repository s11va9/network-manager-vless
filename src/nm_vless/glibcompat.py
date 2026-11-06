# SPDX-License-Identifier: GPL-2.0-or-later
"""Unix signals on both older GLib and the newer GLibUnix namespace."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

GLibUnix: Any = None
try:
    gi.require_version("GLibUnix", "2.0")
except ValueError:
    pass
else:
    GLibUnix = importlib.import_module("gi.repository.GLibUnix")


def unix_signal_add(priority: int, signum: int, handler: Callable[[], bool]) -> int:
    """Watch a Unix signal without using deprecated aliases on newer GLib."""
    if GLibUnix is not None:
        return int(GLibUnix.signal_add(priority, signum, handler))
    return int(GLib.unix_signal_add(priority, signum, handler))
