<!--
SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
SPDX-License-Identifier: MIT
-->

# 0002 — Playback: playbin3, ReplayGain from beets (spike S2)

**Status:** proposed (2026-10-28). The automated check passes, and
playback on the desktop is gapless by ear; the measurements on the
desktop are still to do (see "Still to do on the desktop"), and decide
whether this is accepted.

## Context

`docs/PLAN.md` asks S2 for a minimal engine on `playbin3` under S1's event
loop: a list of files played gapless, ReplayGain from the files' tags and
from the beets database (track and album), seek, position updates and
output through PipeWire, with an automated check that a transition has no
gap and that the gain changes at the right sample. It decides the
ReplayGain source and mechanism, and the engine's threading model.

## What was built

- **The engine** (`roquette/playback/engine.py`): plays a list of
  sources gapless; pause, resume, stop, skip, seek, position, volume;
  reports the source that became audible, state changes, the end and
  errors. A source is a path, ReplayGain values (from beets, or none to
  use the file's tags), a mode (track, album, off) and a length
  (`roquette/playback/source.py`). The gain arithmetic is plain Python
  (`roquette/playback/gain.py`): album mode falls back to the track's
  values and the other way round, a pre-amp is added, a fallback gain
  applies without values, and the peak lowers a gain that would clip.
- **Stand-ins for later milestones**: `load_playables()` and `query_ids()`
  in `roquette/library/index.py` read paths and `rg_*` fields with one
  narrow query (M1); `roquette/queue/replaygain.py` picks album gain for a
  track played next to another of its release, track gain otherwise (M3).
- **The window plays**: a double click (or Return) on a track plays its
  release from that track, on a cover the release. Dragging and the
  context menu still add to the placeholder playlist pane. The transport
  bar drives the engine: play/pause, previous (back to the start after
  3 s), next, seek, volume (on a cubic curve), and the position.
- **Options** until M4's settings: `--play QUERY` (a beets query, played
  once the library is loaded), `--audio-sink`, `--replaygain
  auto|track|album|off`, `--replaygain-source beets|tags`, `--preamp`.
- **`just measure`** measures playing (`PLAY_QUERY`, `PLAY_SECS`,
  `AUDIO_SINK`), with the window shown and minimised (needs `xdotool`),
  and samples wake-ups for every run of it: the context switches of all
  threads and of the main thread. `SCENARIOS` picks the measurements.
- **The automated check** (`tests/test_playback.py`): generated FLAC files
  of pseudo-random samples with odd lengths and ReplayGain tags that
  differ from the "database" values are played through the engine into an
  `appsink` as fast as it takes them. The output must be every input
  sample in order, times its file's gain, within 10⁻⁶: a missing or
  repeated frame, or a gain a sample early or late, fails it. It runs for
  database track and album gain, tags, a mix of both, off, pre-amp and
  fallback, clipping prevention, the same file twice in a row, a file
  name that needs escaping, a seek, and a missing file. It runs in CI and
  takes about a second.

## Findings

### Gapless

- **`about-to-finish` and a new URI is enough.** Every check gave
  exactly as many samples out as went in, at every transition.
- `about-to-finish` comes **early**: once the current file has been read,
  which is seconds before its end (after 1 s of a 3 s file). The next file
  must be known then. If the queue changes after that, the file already
  queued plays next unless the engine switches with a flush (M2/M3).
- The bus's STREAM_START message is posted when the new stream reaches
  the sink, which is when it becomes audible, give or take the sink's
  buffer. The position restarts at 0 there. Its group id is the one the
  stream-start event had, which is how the engine knows which source
  started (see below).
- Seeking in a track after the next one was queued keeps the next one
  queued.
- **An error in the next file cuts the current one short.** The next
  file is opened at `about-to-finish`, so a missing or unreadable one
  fails seconds before the current track ends; stopping then lost the
  last 2 s of a 3 s track in real time. The error names the right file
  (the failing `filesrc`'s location, or a `urisourcebin`'s URI). Not
  stopping lets the current track play to its end, but then no EOS
  comes: the pipeline waits for the stream that failed. M2 skips the
  file instead.

### ReplayGain

- **`rgvolume` reading the tags** changes the gain at the right sample,
  but it negotiates the decoder's integer format (S16 for 16-bit FLAC) and
  then multiplies in fixed point, without dither: up to 4.4 LSB off at
  −3 dB (exact for −6 dB, a factor of ½). Forcing 32-bit floats in front
  of it makes it exact. Its album mode is one property for the whole
  pipeline, though: choosing album or track gain per track, as the plan's
  "auto" mode does, would need a probe at each transition anyway.
- **The beets database as the source works sample-exactly**: a `volume`
  element after a conversion to 32-bit floats, and a pad probe on it that
  sets its factor when a stream starts, before the stream's first sample
  reaches it.
- **Which file a stream is**: the stream-id at the filter starts with the
  SHA-256 of the URI the file was opened with (GStreamer's
  `gst_pad_create_stream_id()` for sources that are URI handlers);
  containers append `/<n>`. Checked with FLAC, MP3, Ogg Vorbis, Opus and
  M4A, and a name with spaces, `'`, `#`, `%`, `&`, brackets and non-ASCII
  letters (the URI is made with `Gst.filename_to_uri()`, as `filesrc`
  makes it). The same file twice in a row is told apart by the order the
  URIs were set in.
- **The files' tags as a fallback** for tracks beets has no values for:
  they come before the first sample in the formats tried (stream headers).
  A one-shot buffer probe closes that window, so the gain never changes
  in the middle of a track; a file whose tags came later would keep the
  fallback gain.
- **Clipping prevention through the peaks** (`gain ≤ 1 / peak`), not
  `rglimiter`: that one limits with a soft knee from −6 dBFS, so it
  changes loud passages that wouldn't clip.
- **Tags are dropped** at the filter once read: roquette takes metadata
  from beets, and the sink posts every tag event as a bus message — about
  one a second for FLAC (bitrate updates), more for MP3 and Vorbis.
  Playing 20 s of FLAC woke the event loop 1.35 times a second instead of
  1.95.

### Threads

- **No GLib main loop**: the bus's file descriptor (`Gst.Bus.get_pollfd()`)
  is watched with `add_reader` on the asyncio loop that Slint runs, and
  messages are popped there. The engine's callbacks therefore run on the
  event loop's thread and can update the window directly.
- **Python on streaming threads**: `about-to-finish` once per track, the
  gain probe once per event (stream-start, caps, segment, tags: about one
  a second for FLAC), the one-shot buffer probe once per stream that waits
  for its tags. Never once per buffer. Each takes the GIL, which a busy
  event loop gives up within the interpreter's switch interval (5 ms),
  far less than the sink's buffer. PyGObject makes a new thread state for
  each such call, which is cheap.
- **The lock** shared with the streaming threads is never held while the
  pipeline changes state: going to READY waits for the streaming threads,
  which may be waiting for the lock.

### Wake-ups and the position display

In the container (see "Measurements"), the main thread doesn't wake up
while idle, as in S1, nor while playing without the position display;
the bus's few messages don't show. The position display is what wakes
it:

- Every change to the transport bar renders two to three frames with the
  software renderer, even when only the time's text changes (the later
  frames have nothing dirty), and each frame wakes the main thread two to
  three times.
- Updating four times a second cost 17.8 wake-ups a second. The display
  now updates once a second, just after each whole second of the track,
  when the text changes anyway: **9.2 a second**, and 1.3 % CPU in all
  while playing instead of 2.5 %.
- **A hidden window can't be detected from Python**: slint 1.18.1b1's
  Python API has no property or callback for being minimised or occluded,
  and an unmapped window under Xvfb went on rendering three frames a
  second. Until it can, the display keeps updating while hidden, against
  the budget's "none while the window is hidden or minimised" (M5).
- GStreamer's own threads (two `multiqueue`s, the parser, the sink's
  queue) wake about ten times a second each: FLAC frames are 4,096
  samples, 93 ms. Larger buffers would wake them less (M2).

### Typing

`pygobject-stubs` (2.20.0, LGPL-2.1) would type the GStreamer calls. On
the engine and its test it reports 21 errors: properties of plugins
(`playbin3.props.uri`), which it can't know, and `Optional` results that
need checks. It would have caught a misnamed method
(`ElementFactory.get_klass()`) that only showed at run time.

## Measurements

In the cloud container under Xvfb (scale 1, llvmpipe for OpenGL), with a
generated library and `AUDIO_SINK="fakesink sync=true"` (no sound card),
playing a FLAC album at 44.1 kHz: Intel Xeon @ 2.80 GHz (2 cores),
Ubuntu 24.04, GStreamer 1.24.2, Python 3.13.18, slint 1.18.1b1, beets
2.14.1. Idle: 20 s; playing: 30 s after 5 s settling.

| renderer | what | CPU % | wake-ups/s (all / main) | RSS MiB | PSS MiB | threads |
|---|---|---|---|---|---|---|
| software | window, idle | 0.0 | – | 110.7 | 105.3 | 6 |
| software | window, playing | 1.3 | 43.9 / 9.5 | 116.3 | 110.9 | 14 |
| femtovg | window, idle | 0.0 | – | 227.0 | 202.8 | 11 |
| femtovg | window, playing | 3.2 | 63.4 / 16.6 | 235.5 | 211.2 | 19 |

Playing adds about 6–9 MiB RSS and eight threads (GStreamer's). FemtoVG on
llvmpipe renders on the CPU, so its frames cost more here than on the
desktop's GPU. "Minimised" couldn't be measured: Xvfb has no window
manager to minimise to.

## Still to do on the desktop

- **The listening test**: a few releases known to be gapless (live albums,
  DJ mixes, classical works with continuous movements), played through
  with `just run --play 'album:…'`, listening for gaps, clicks and gain
  jumps at the transitions, in `--replaygain auto` (album gain). Done for
  gaps: none heard on the desktop.
- **`just measure`** with `PLAY_QUERY` set to a release longer than
  `SETTLE_SECS` + `PLAY_SECS` (40 s by default) and `xdotool` installed:
  CPU and wake-ups while playing, window shown and minimised, and which
  sink autoaudiosink picks (pipewiresink, or pulsesink through
  pipewire-pulse). Also with `AUDIO_SINK=pipewiresink` and
  `AUDIO_SINK=pulsesink` for comparison.
- How many items of the dev library have no `rg_*` values in beets (the
  tags fallback, or the fallback gain, applies to them).

## Decision (proposed)

- **ReplayGain source: the beets database**, so a `beet replaygain` run is
  followed without depending on file tags. Files beets has no values for
  use their own tags; `--replaygain-source tags` uses the tags for all.
- **Mechanism: a `volume` element on 32-bit floats** in `playbin3`'s audio
  filter, set by a pad probe when a stream starts, the stream identified
  by the hash of its URI. Not `rgvolume`, and no `rglimiter`: the peak
  limits the gain. The mode (track, album, off) is chosen per track by
  whoever queues it.
- **Threading**: the control methods and callbacks on the event loop's
  thread; the bus read on the asyncio loop through its file descriptor;
  on streaming threads, only `about-to-finish` (setting the next URI from
  what the queue resolved ahead) and probes on events, never on buffers.
- **The position display** updates once a second while playing, and not
  at all while paused or stopped.

## Consequences

- M2 hardens this engine: the state machine, errors (skip a missing or
  unreadable file and report it, instead of stopping; when it's the next
  file, the current track plays to its end first), output device
  selection, a switch with a flush when the queue changes after
  `about-to-finish`, buffer sizes for fewer wake-ups, and
  `pygobject-stubs` with checks or casts where they need them.
- M3's queue gives the engine the next source synchronously, from a
  streaming thread, without the event loop (the engine's list is S2's
  stand-in), and decides the mode per track.
- M5: hidden windows (ask Slint for a visibility or occlusion property in
  Python), and why a change renders two to three frames.
- The plan's "Playing" budget gets the desktop's numbers.
- Open issues:
  - A flushing seek while the next file is already past the filter but
    not yet audible (the sink's buffer, a fraction of a second at the end
    of a track) would keep the next file's gain for the rest of the
    current one. Not seen; M2 can restore the audible source's gain on
    flush.
  - The hash of the URI as stream-id is how GStreamer makes stream-ids
    for URI handlers, not a documented guarantee. If it changes, the
    engine falls back to the order the URIs were set in and warns.
  - R128 gains (`r128_*`, Opus) aren't used; the library has no Opus.
  - Whether autoaudiosink picks PipeWire directly, and whether that
    matters for CPU, latency or wake-ups (desktop).
