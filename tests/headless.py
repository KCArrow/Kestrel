#!/usr/bin/env python3
"""Run kestrel.py without a display or GTK: real GLib/Gio (file ops, content types, cancellables) but
GTK/GDK/Pango stubbed out. Used for `--selftest` in CI and sandboxes:

    python3 tests/headless.py --selftest [--bench]
"""
import os
import sys
import types
from unittest.mock import MagicMock

import gi

_real_require = gi.require_version


def _require(ns, ver):
    try:
        _real_require(ns, ver)
    except ValueError:
        pass            # stubbed namespace


gi.require_version = _require


class _Widget:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        return MagicMock(name=name)


class _Mod(MagicMock):
    _widgets = ("DrawingArea", "GLArea", "Box", "Layout", "ApplicationWindow", "Window", "Dialog", "TreeView",
                "Application", "Label", "ScrolledWindow", "Frame", "Button", "ToggleButton", "Stack", "Paned")

    def __getattr__(self, name):
        if name in self._widgets:
            return type(name, (_Widget,), {})
        return super().__getattr__(name)


import gi.repository  # noqa: E402  (the real dynamic importer package)
from gi.repository import GLib, Gio  # noqa: E402  (real)

for ns in ("Gtk", "Gdk", "GdkPixbuf", "Pango", "PangoCairo", "GtkSource", "Gst"):
    m = _Mod(name=ns)
    sys.modules[f"gi.repository.{ns}"] = m
    setattr(gi.repository, ns, m)

here = os.path.dirname(os.path.abspath(__file__))
target = os.path.join(os.path.dirname(here), "kestrel.py")
sys.argv = [target] + sys.argv[1:]
import runpy  # noqa: E402

runpy.run_path(target, run_name="__main__")
