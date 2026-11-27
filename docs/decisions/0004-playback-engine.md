<!--
SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
SPDX-License-Identifier: MIT
-->

# 0004 — The playback engine (M2)

**Status:** proposed (2026-11-25).

## Context

M2 hardens S2's engine (`docs/decisions/0002-playback.md`, "Consequences"):
a state machine, errors that skip the file instead of stopping, the next
source from the live playlist (M3) instead of a list, a switch when the
playlist changes after `about-to-finish`, output device selection,
buffer sizes for fewer wake-ups, and `pygobject-stubs`. The plan adds a
volume above 100% and, with each track that starts, the ReplayGain
applied to it, for the volume control (M5).

M2 was built in a cloud session (GStreamer 1.24.2, Ubuntu 24.04),
without a sound card or the dev library. What GStreamer does at the
edges was found by experiment there first; the tests then play
generated FLAC files into an `appsink`, as S2's did. CI's runners have
GStreamer 1.28.2 (Ubuntu 26.04), where the tests pass too.

## Findings

### Where the next stream is

The audio filter, where the gain probe sits, is *before* a queue in
playsink (`aqueue`: up to 1 s or 200 buffers), and that is before the
sink and its own buffer. The next file's stream therefore passes the
filter about a second plus the sink's buffer before it's heard, not "a
fraction of a second" as S2 assumed. With 3 s files and a sink synced to
the clock: `about-to-finish` at 0.96 s, the next stream at the filter at
1.80 s, audible at 2.95 s.

That window matters three ways:

- **A seek in it goes to the next file.** playbin3 seeks the stream at
  the decoder, which is the next file's once it has passed the filter:
  seeking to 1.0 s, 0.8 s before the end of a 3 s track, played the
  *next* track from 1.0 s and then ended. Before the window (after
  `about-to-finish`, with the next file only queued) the seek goes to
  the audible file and the next one stays queued, as S2 found. (S2's
  open issue, the next file's gain kept for the rest of the audible
  one, was the smaller half of this.)
- **A URI set after another one was set is ignored**: once
  `about-to-finish` has queued a file, setting `uri` again doesn't
  replace it; the first one plays.
- **A URI set after `about-to-finish` set none is taken**, gaplessly, as
  long as the audible file's end-of-stream hasn't passed the filter.
  Set later, the new stream starts after the end-of-stream at the
  filter, and playbin3 posts EOS.

### Errors

- A next file that can't be played (missing, a directory, a text file,
  a truncated FLAC) posts two to six ERROR messages (the specific one
  first, then "Internal data stream error" or "state change failed")
  and leaves the pipeline waiting for the stream: the audible file
  plays to its end, then nothing, and no EOS comes. A URI set from the
  event loop then is taken gaplessly, as if `about-to-finish` had set it
  (1.24). On CI's 1.28 it once wasn't: the audible track ended and the
  new file never started.
- A corrupt FLAC (garbage in its frames) only posts warnings: it plays,
  with whatever the decoder makes of it.
- A `filesrc` that failed has no parent any more when its message is
  handled: its location says which file it was. Errors from typefind
  and parsers name the `urisourcebin` above them, whose URI does.
- **A next file that fails while the pipeline starts fails the start.**
  A file is read to its end before the pipeline has prerolled when it's
  shorter than the queues hold (about 1.5 s) or opened near its end;
  `about-to-finish` comes then, and the next file's source fails its
  state change, which fails the pipeline's: it stays in READY with
  PLAYING pending. That playbin3 can't be started again: from READY or
  NULL, at once or later, it aborts the process on an assertion
  (`gstplaybin3.c:2617:reconfigure_output: assertion failed:
  (combine->sinkpad == NULL)`). A new playbin3 with the same sink plays.
- `get_state()` returns FAILURE after a next file failed while playing
  too, but the pipeline is PLAYING and plays on, and starts again
  normally.

### A start that never finishes

With all cores busy (six busy loops on four), playbin3 sometimes stays in
READY with PLAYING pending and posts nothing, within a few starts of a
short file; with no Python callbacks at all too, so it's GStreamer's.
Without load, 160 starts in a row never did. GStreamer 1.28 hasn't been
tried under load.

### Memory per track

A bare playbin3 (no roquette, no Python callbacks) grows by about
0.1 MiB of RSS per track: 400 gapless tracks of 0.2 s took it from 45 to
88 MiB, and 200 starts from READY from 45 to 67 MiB, the same through
NULL. The engine grows at the same rate, replacing the pipeline makes no
difference, and a start that fails doesn't grow it. The elements don't
pile up in the pipeline (one `urisourcebin` at a time). About 12 MiB a
day of music at four minutes a track.

**GStreamer 1.28 doesn't** (on the desktop, see "Measurements"): about
130 more tracks in the same process added 0.6 MiB. It's 1.24's, which
the plan's "RSS flat over an 8-hour run" doesn't have to answer for.

### A pause in a track's last moments

On the desktop: paused in the last second of a track, the transport
bar showed the next track (its title, its duration) while the end of
the previous one was still to be heard. The sink takes the next stream
once the rest of the previous one is in its buffer (0.4 s), and posts
STREAM_START then, paused or not; the engine reported the track change
at once. (The seek bar then stopped on its way back to 0, and stayed
there after "previous": the Slider's animation, which seems to need
frames that nothing asks for while paused. M5's seek bar.)

### Wake-ups and the sink's buffer

`scripts/measure-playback.py` (`just measure-playback`, new) plays
generated FLAC files in real time without the window and counts the
wake-ups of every thread, by name (see "Measurements"), which S2 couldn't
break down:

- **pulsesink's client thread** (`threaded-ml`, PulseAudio's threaded
  main loop) is the busiest. On the desktop, through PipeWire's pulse
  server: 92 a second with GStreamer's 10 ms segments, 20 with 100 ms
  ones, and playsink's queue, which feeds it, wakes 43 times a second
  instead of 9. In the container, PulseAudio's null sink asked for data
  in larger pieces than the segment anyway: 31 against 21.
- **GStreamer's queues** wake per buffer: decodebin3's multiqueue
  (`multiqueue0:src`, about 10 a second, one per FLAC frame of 4,096
  samples) and playsink's queue (`aqueue:src`, 10 to 13). Bigger buffers
  would need a different pipeline; they aren't the sink's to change.
- The main thread (the event loop) wakes about three times a second
  without the window: the bus's messages around each transition (every
  7 s here) and the measuring itself.
- CPU is 0.7 % of a core either way, which is decoding and the queues;
  the plan's "well under 1 %" for FLAC holds for the engine on its own.

### Listening test

On the desktop, gapless releases with the new sink sizes (0.4 s buffer,
0.1 s segments, pulsesink through PipeWire): no gaps, clicks or gain
jumps at the transitions. A missing file wasn't tried there: that would
mean changing files of the music library.

## Decision

- **The next source comes from a provider** (`Upcoming.next_source`),
  asked on the streaming thread at `about-to-finish` without the engine's
  lock held: the live playlist (M3); a `SourceList` until then.
  `next_changed()` tells the engine the answer may have changed, and the
  engine follows it where the next stream is:
  - not queued yet: nothing to do;
  - `about-to-finish` found nothing: the new one is queued late (gapless
    while it can be, otherwise after the end-of-stream);
  - queued, not at the filter: when its stream reaches the filter, the
    filter drops it and sends an end-of-stream instead; the audible
    track plays to its end, then the new next one starts (a gap of a
    new start);
  - past the filter: it's skipped when it becomes audible; a fraction of
    a second of it may be heard.
- **State machine**: the state asked for and the state reached are kept
  apart. Only the state reached is reported, and only when it's the one
  asked for, so starting, reopening or playing from a position don't
  flicker through PAUSED; pause and resume work while the pipeline is
  still on its way.
- **A seek while the next stream is past the filter reopens the audible
  file** at the position, without reporting it as started again. Changing
  the output (`set_sink`) does the same.
- **Errors are skipped and reported** (`PlaybackError` with a `Problem`:
  missing, unreadable, unsupported, decode, other, and what plays
  instead). A failing next file lets the audible one play to its end
  (and what of the failed one has passed the filter already, which isn't
  reported as started): the file after the failed one is queued late,
  gaplessly. Until something after it starts, a timer watches the end of
  the audible file (at its remaining time, then a quarter of a second
  later): once its position stands still, the engine starts the queued
  file itself, if GStreamer didn't, or what follows. Sources that failed
  are skipped until the next `play()`; ten failures in a row stop
  playback, once the audible file has ended where they were next files.
- **Against playbin3 1.24's failed starts**: `about-to-finish` leaves the
  next source to the event loop until the pipeline has reached the state
  it was started in, then it's queued late. Gapless unless the first
  file's end has passed the filter by then (a first track under about a
  second). A pipeline that failed to start anyway is replaced by a new
  one with the same sink.
- **A start watchdog**: a pipeline that hasn't reached its state 5 s after
  a start is replaced and started again, three tries in all, each waiting
  5 s longer than the one before, then it's reported as an error. A start
  that is only slow (a disk spinning up, a network mount) looks the same,
  and gets 30 s in all.
- **The gain is reported with each track that starts**
  (`on_started(source, AppliedGain)`): the factor, the mode asked for and
  the values used, where they came from, the gain with the pre-amp
  before the peak limits it, the peak, whether it limited the gain, and
  the volume at which the track would clip. With the gain from the tags,
  it may be known only after the sink posted STREAM_START: the probe
  posts an application message, and the report waits for both.
- **A track change that reaches the sink while paused** is reported when
  playback resumes. Until then `current`, `gain` and `position()` are
  the track shown, where it was paused, and a seek opens that track
  again at the position.
- **Volume**: 0 to 10 (playbin3's range), linear; above 1 it amplifies.
- **Outputs are sink descriptions**: `list_outputs()` turns GStreamer's
  devices into the element and the properties that pick the device
  (`pulsesink device="…"`), which `--audio-sink`, and M4's setting, take.
  `roquette --list-outputs` prints them. pipewiresink's element for a
  device targets the node's serial (`target-object="4812"` on the
  desktop), a number PipeWire hands out while it runs, which a restart
  or a replug can give to another device; the description names the
  node instead (`node.name`, which `target-object` takes too), e.g.
  `pipewiresink target-object="alsa_output.usb-YAMAHA_Yamaha_NX-N500-00.analog-stereo"`
  on the desktop.
- **The sink's buffer and segments: 0.4 s and 0.1 s** for sinks that have
  them (pulsesink, alsasink), instead of GStreamer's 0.2 s and 10 ms,
  set on every element playbin3 adds (`element-setup`), so autoaudiosink's
  sink too: 62 % fewer wake-ups in all with pulsesink through PipeWire
  on the desktop (see "Measurements"), for a buffer that's 0.2 s
  longer. autoaudiosink's pulsesink stays the default: with these sizes
  it wakes about half as often as pipewiresink, which has neither
  property. `--audio-buffer` and
  `--audio-latency` change them until M4.
- **`pygobject-stubs`** in the lint group: mypy checks the GStreamer
  calls. Plugins' properties, which the stubs can't know, are set by
  name; results that may be None are checked. Without the stubs mypy
  still passes.

## Measurements

### On the desktop

i7-1185G7, Iris Xe, KDE on X11, Linux 7.3.5, GStreamer 1.28.9, Python
3.12.16, slint 1.19.0b1, beets 2.14.1, the dev library (11,849 tracks);
at 71d748a, 2026-11-26.

`just measure-playback`, 30 s after 5 s, three runs each; autoaudiosink
picked pulsesink (through PipeWire's pulse server):

| buffer / segments | CPU % | wake-ups/s (all / main) | RSS MiB | PSS MiB | threads |
|---|---|---|---|---|---|
| 0.2 s / 10 ms (M1) | 0.7–0.8 | 173.5–175.0 / 2.6 | 49.6–51.1 | 30.6–32.1 | 13 |
| 0.4 s / 100 ms (M2) | 0.6–0.7 | 66.6–67.1 / 2.1–2.7 | 49.5–51.2 | 30.5–32.1 | 13 |

| thread | 10 ms | 100 ms |
|---|---|---|
| `threaded-ml` (PulseAudio client) | 92.0 | 20.0 |
| `aqueue:src` (playsink) | 43.1 | 8.6 |
| `multiqueue0:src` (decodebin3) | 10.3 | 9.2 |
| `typefindelement` | 9.6 | 9.4 |
| main thread | 2.6 | 2.7 |

One run of 60 s and one of 900 s (about 130 more tracks): RSS 50.1 and
50.7 MiB, PSS 31.0 and 31.7 MiB.

`just measure femtovg` with `PLAY_QUERY='album:Stripped'`, against M1's
run at 1fa073b (desktop, slint 1.18.1b1, from M1's pull request):

| | M1 | M2, pulsesink | M2, pipewiresink |
|---|---|---|---|
| window created / library loaded | 362 / 512 ms | 324 / 446 ms | 339 / 476 ms |
| idle RSS / PSS | 197.8 / 126.3 MiB | 198.4 / 127.2 MiB | 198.1 / 127.2 MiB |
| playing CPU, shown / hidden | 2.3 / 0.9 % | 1.7 / 0.5 % | 1.8 / 0.7 % |
| playing wake-ups/s (all / main), shown | – | 79.3 / 15.1 | 149.0 / 13.7 |
| playing wake-ups/s (all / main), hidden | – | 69.8 / 7.1 | 140.0 / 6.7 |
| scrolling tracks (fps median / min) | 49 / 45 | 45 / 35 | 45 / 41 |
| tracks, 10× | 28 / 24 | 25 / 22 | 25 / 23 |
| covers, cold / warm cache | 55 / 56 | 58 / 57 | 57 / 57 |

Playing costs less than in M1 and wakes far less than S2's 180 a second
with pulsesink. Startup is faster and the library is loaded within the
500 ms budget, and scrolling the track list a little slower: M2 doesn't
touch either, slint 1.19.0b1 does (M5).

### In the container

`just measure-playback` in the cloud container (Intel Xeon @ 2.80 GHz,
4 cores, Ubuntu 24.04, GStreamer 1.24.2, Python 3.13.18), three runs of
30 s after 5 s each, FLAC at 44.1 kHz with a transition every 7 s.
pulsesink plays to a PulseAudio 16.1 server in the container with a null
sink; 0.2 s / 10 ms is what S2's and M1's engine had.

| sink | buffer / segments | CPU % | wake-ups/s (all / main) | RSS MiB | PSS MiB | threads |
|---|---|---|---|---|---|---|
| pulsesink | 0.2 s / 10 ms | 0.7 | 73.2–76.3 / 3.0–3.5 | 43.2–45.5 | 40.6–42.8 | 10–13 |
| pulsesink | 0.4 s / 100 ms | 0.7 | 60.5–62.1 / 3.1–3.3 | 42.5–45.1 | 39.8–42.4 | 13 |
| fakesink sync=true | – | 0.6–0.7 | 30.3–30.5 / 2.7–2.8 | 39.6–41.7 | 37.7–39.8 | 9 |

Wake-ups a second by thread, first run of each:

| thread | pulsesink, 10 ms | pulsesink, 100 ms | fakesink |
|---|---|---|---|
| `threaded-ml` (PulseAudio client) | 31.4 | 21.3 | – |
| `multiqueue0:src` (decodebin3) | 17.7 | 10.1 | 9.9 |
| `aqueue:src` (playsink) | 11.2 | 13.4 | 9.6 |
| `typefindelement` | 3.6 | 6.7 | 2.8 |
| main thread | 3.5 | 3.3 | 2.7 |
| other multiqueues, parsers | 5.3 | 5.8 | 4.8 |

RSS grows by about 1 MiB from run to run, within the process: the ten
tracks each run plays (see "Memory per track").

`just measure` (startup and idle) under Xvfb on the fake library, M1
(`master`) and M2 in the same container: no difference beyond the runs'
spread (window created 290 / 291 ms with FemtoVG, library loaded 444 /
442 ms; idle 244.4 / 240.0 MiB RSS with FemtoVG, 119.1 / 119.3 MiB with
the software renderer). The playing scenario needs the dev library's
music, on the desktop.

## Consequences

- **M3**: the live playlist implements `Upcoming` (thread-safe, without
  calling the engine), calls `next_changed()` on every change that can
  affect what follows the playing entry, and owns the current entry:
  `stop()` forgets it. "Stop after the current track" is the provider
  answering None, and `next_changed()`.
- **M4**: the output (a sink description), the buffer and the segment
  size become settings.
- **M5**: while playing, `on_started` comes about the sink's buffer
  before the track is heard (the sink takes the next stream once the
  previous one is in its buffer): 0.4 s now, 0.2 s before. The display
  can wait for it. The seek bar's Slider stops in the middle of its
  animation while paused. The volume control has `AppliedGain` to show.
- Open issues:
  - Whether GStreamer 1.28 still fails starts as 1.24 does (the tests
    pass on CI's 1.28, which doesn't tell); the workarounds cost nothing
    when it doesn't.
  - The stale stream past the filter can be heard for a moment; the
    first transition after a start isn't gapless for a first track
    under about a second.
  - GStreamer's own threads wake per FLAC frame (93 ms) and per buffer
    of playsink's queue; larger buffers would need a different
    pipeline.
  - playbin3 1.24 grows by 0.1 MiB a track, 1.28 doesn't (see "Memory
    per track"); on a system with 1.24, as Ubuntu 24.04 has, a long run
    grows.
