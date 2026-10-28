# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Playback with GStreamer: the engine (`engine`) and ReplayGain (`gain`).

`gain` doesn't import GStreamer. `engine` does, so it's imported where
the engine is created: on a worker thread, as loading GStreamer blocks.
"""
