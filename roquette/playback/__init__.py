# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Playback with GStreamer.

S1 only loads GStreamer, so the window is measured with it; S2 builds the
engine.
"""

from typing import Any


def load_gstreamer() -> Any:
    """Initialise GStreamer and create the playbin3 S2 will play with.

    Blocking (the registry is read, plugins are loaded), so for a worker
    thread.
    """
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    return Gst.ElementFactory.make("playbin3", "player")
