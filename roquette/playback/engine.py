# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The playback engine: GStreamer's `playbin3`, gapless, with ReplayGain.

The engine plays one source and asks its provider (`Upcoming`: the live
playlist, M3) what follows, when GStreamer needs to know. When the
provider's answer changes, `next_changed()` tells the engine, which
follows it (see "The next source").

Threads (docs/decisions/0002-playback.md):

- The control methods and the callbacks run on the event loop's thread.
  GStreamer's bus is watched through its file descriptor on the asyncio
  loop, so its messages are handled there without a GLib main loop or a
  thread of their own.
- `about-to-finish` is emitted on a streaming thread once the current
  file has been read, well before it ends. The provider is asked for the
  next source right there and its URI set, without waiting for the event
  loop.
- The gain is applied by a `volume` element in the audio filter, working
  on 32-bit floats. A pad probe on it sees the events (not the buffers)
  of each stream on its streaming thread and sets the gain when a stream
  starts, before its first sample. A stream's id starts with the SHA-256
  of its URI, which says which file it is; its group id, which the bus's
  STREAM_START message repeats when the stream reaches the sink, says
  when it becomes audible.
- The lock shared with the streaming threads is never held while the
  pipeline changes state (going to READY waits for the streaming threads,
  which may be waiting for the lock) or while the provider is called.

The next source: what follows the audible source goes through the
filter, a queue in playsink (a second of audio) and the sink's buffer
before it's heard. Where it is decides what a change needs:

- Not queued yet (before `about-to-finish`): nothing, the provider is
  asked then. After `about-to-finish` found nothing, the new next source
  is queued late, which is gapless while the audible file's data hasn't
  all passed the filter; otherwise it starts after the end.
- Queued, but its stream hasn't reached the filter: when it does, the
  filter drops it and sends an end-of-stream instead, so the audible
  source plays to its end; the provider's next source then starts.
- Past the filter (in the last second or so): it's skipped when it
  becomes audible, a fraction of a second of it may be heard.

A seek in that last second would go to the next file's stream, which is
the one at the decoder: the engine opens the audible file again at the
position instead. So does a change of the output.

Errors: a source that fails is reported and skipped. When it's the next
one, opened while the audible one still plays, that one plays to its end
first: the provider's source after the failed one is queued in its
place, which GStreamer takes gaplessly.
"""

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from roquette.playback.gain import AppliedGain, Mode, Origin, ReplayGain, apply  # noqa: E402
from roquette.playback.source import (  # noqa: E402
    PlaybackError,
    Problem,
    Source,
    State,
    Upcoming,
)

__all__ = ["MAX_SKIPS", "MAX_VOLUME", "Engine", "Source", "State"]

log = logging.getLogger(__name__)

# GstPlayFlags: audio only, with a software volume where the sink has none.
_PLAY_FLAGS = 0x02 | 0x10

# The gain is applied to floats: on integer samples `volume` multiplies in
# fixed point, without dither.
_FILTER = "audioconvert ! audio/x-raw,format=F32LE ! volume name=replaygain"

# The probe tells the event loop that a stream's gain is known, when it
# came from the stream's tags.
_GAIN_MESSAGE = "roquette-gain"

# Failures in a row after which the engine stops instead of skipping on:
# a playlist on a disk that isn't mounted fails for every file.
MAX_SKIPS = 10

# `playbin3`'s volume goes to 10 (+20 dB); above 1 it amplifies, in the
# sound server for sinks with a stream volume (PulseAudio, PipeWire).
MAX_VOLUME = 10.0

# The audio sink's buffer and the size of its segments, in seconds, for
# sinks that have them (pulsesink, alsasink). GStreamer's defaults are
# 0.2 s and 0.01 s; with pulsesink, 0.1 s segments wake its thread 21
# times a second instead of 31 (docs/decisions/0004-playback-engine.md).
# Track changes show about a buffer early, as the sink takes the next
# stream once the previous one is in its buffer.
BUFFER_TIME = 0.4
LATENCY_TIME = 0.1

# While a next file has failed: how long after the audible source's end
# the timer looks again whether its position still moves.
_TAIL_CHECK = 0.25

# GStreamer 1.24's playbin3 sometimes doesn't start: it stays in READY
# with PLAYING pending and posts nothing (seen with every core busy,
# without roquette's callbacks too). A pipeline that hasn't started after
# this many seconds is replaced and started again, up to `_START_TRIES`
# times in all.
START_TIMEOUT = 5.0
_START_TRIES = 3


@dataclass(eq=False, slots=True)
class _Entry:
    """A source whose URI was set, from its stream's start to its end."""

    source: Source
    key: str
    # The audible source opened again (a seek, a new output): not reported
    # as started again.
    restart: bool = False
    # Set by the gain probe when its stream starts at the filter.
    started: bool = False
    group: int | None = None
    gain: AppliedGain | None = None
    # The gain doesn't change any more (it was waiting for the tags).
    gain_final: bool = False
    # The provider has another source next; the filter dropped its stream.
    stale: bool = False
    dropped: bool = False
    # The event loop's: it failed; on_started was called.
    failed: bool = False
    reported: bool = False


def stream_key(uri: str) -> str:
    """The start of the stream-id GStreamer gives a stream read from `uri`."""
    return hashlib.sha256(uri.encode()).hexdigest()


def make_sink(description: str) -> Any:
    """A sink from an element's name or a description, such as
    "pulsesink device=…" or "fakesink sync=true"."""
    sink: Gst.Element | None
    if " " in description or "!" in description:
        sink = Gst.parse_bin_from_description(description, True)
    else:
        sink = Gst.ElementFactory.make(description)
    if sink is None:
        raise RuntimeError(f"no GStreamer element {description!r}")
    return sink


class Engine:
    """Plays a source and what the provider has after it, gapless.

    Blocking to create (GStreamer's registry is read, plugins are loaded),
    so for a worker thread; `attach` then hands it to the event loop.
    """

    def __init__(
        self,
        sink: Any = None,
        *,
        preamp: float = 0.0,
        fallback: float = 0.0,
        buffer_time: float | None = BUFFER_TIME,
        latency_time: float | None = LATENCY_TIME,
    ) -> None:
        Gst.init([])
        self.preamp = preamp
        self.fallback = fallback
        self.buffer_time = buffer_time
        self.latency_time = latency_time
        # What plays after a source; without one, a source plays alone.
        self.upcoming: Upcoming | None = None
        # A source became audible, with the gain applied to it. Not called
        # again for the same source after a seek or a change of output.
        self.on_started: Callable[[Source, AppliedGain], None] | None = None
        self.on_state_changed: Callable[[State], None] | None = None
        # Playback ended: the last source ended or failed and the provider
        # had nothing after it, or too many failed in a row (`MAX_SKIPS`).
        self.on_finished: Callable[[], None] | None = None
        self.on_error: Callable[[PlaybackError], None] | None = None

        if isinstance(sink, str):
            sink = make_sink(sink)
        self._sink = sink
        self._bus_fd: int | None = None
        self._loop: Any = None
        self._build()

        # Shared with the streaming threads.
        self._lock = threading.Lock()
        # From the audible source on (or the first one, before anything is
        # audible), in the order their URIs were set.
        self._entries: list[_Entry] = []
        # `about-to-finish` found nothing after the last entry, or the last
        # entry failed: a new next source is queued late.
        self._exhausted = False
        # An end-of-stream passed the filter: nothing can be queued late.
        self._eos = False
        # The pipeline has reached the state it was started in. Until then,
        # `about-to-finish` leaves the next source to the event loop: a
        # next file that fails while the pipeline starts fails the start.
        self._settled = False
        self._deferred = False
        # The filter drops the stream that passes it (a stale one).
        self._dropping = False
        # The sources that failed since `play`, which the provider's answers
        # skip: a file opened again isn't tried again.
        self._failed: list[Source] = []
        # Only touched by the gain probe: the stream that passes it while
        # its gain is still to come from its tags, and its mode.
        self._awaiting: tuple[_Entry | None, Mode] | None = None

        # The event loop's.
        self._state = State.STOPPED
        # The state asked for, which the pipeline may not have reached yet.
        self._target = State.STOPPED
        self._current: _Entry | None = None
        # Where to seek once the pipeline has paused (a source opened
        # again, or at a position).
        self._seek_to: float | None = None
        # Failures in a row.
        self._errors = 0
        # A next file failed while the one before played: GStreamer posts
        # no end-of-stream then, and doesn't always take the file queued in
        # its place (1.28). A timer watches the end of the audible source
        # until something after it starts.
        self._stalled = False
        self._tail_timer: Any = None
        # The start that is waiting for the pipeline, and its tries.
        self._start_timer: Any = None
        self._start_tries = 0

    # Event loop

    def attach(self, loop: Any) -> None:
        """Handle the bus's messages on `loop` (an asyncio event loop)."""
        self._loop = loop
        self._watch_bus()

    def close(self) -> None:
        self._cancel_tail()
        self._cancel_start()
        self._playbin.set_state(Gst.State.NULL)
        self._unwatch_bus()

    def _build(self) -> None:
        """Make the pipeline."""
        playbin = Gst.ElementFactory.make("playbin3")
        if not isinstance(playbin, Gst.Pipeline):
            raise RuntimeError("GStreamer's playbin3 isn't available")
        # playbin3's own properties aren't known to the type stubs.
        playbin.set_property("flags", _PLAY_FLAGS)
        if self._sink is not None:
            playbin.set_property("audio-sink", self._sink)
        audio_filter = Gst.parse_bin_from_description(_FILTER, True)
        playbin.set_property("audio-filter", audio_filter)
        volume = audio_filter.get_by_name("replaygain")
        bus = playbin.get_bus()
        assert volume is not None and bus is not None
        sink_pad, src_pad = volume.get_static_pad("sink"), volume.get_static_pad("src")
        assert sink_pad is not None and src_pad is not None
        sink_pad.add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM | Gst.PadProbeType.EVENT_FLUSH,
            self._probe,
        )
        playbin.connect("about-to-finish", self._about_to_finish)
        playbin.connect("element-setup", self._element_setup)
        self._playbin: Gst.Pipeline = playbin
        self._volume: Gst.Element = volume
        self._gain_pad: Gst.Pad = sink_pad
        self._gain_src: Gst.Pad = src_pad
        self._bus: Gst.Bus = bus

    def _rebuild(self) -> None:
        """Replace the pipeline after a failed state change.

        playbin3 (1.24) doesn't recover from one: started again, even
        from NULL, it aborts the process on an assertion
        (`reconfigure_output: combine->sinkpad == NULL`).
        """
        log.info("a state change failed; making a new pipeline")
        volume = self.volume
        self._playbin.set_state(Gst.State.NULL)
        self._unwatch_bus()
        # The sink goes to the new pipeline.
        self._playbin.set_property("audio-sink", None)
        self._build()
        self.volume = volume
        if self._loop is not None:
            self._watch_bus()

    def _watch_bus(self) -> None:
        self._bus_fd = self._bus.get_pollfd().fd
        self._loop.add_reader(self._bus_fd, self._read_bus)

    def _unwatch_bus(self) -> None:
        if self._bus_fd is not None:
            self._loop.remove_reader(self._bus_fd)
            self._bus_fd = None

    # Control, on the event loop's thread

    @property
    def state(self) -> State:
        """The state the pipeline reached (it may be on its way to another)."""
        return self._state

    @property
    def current(self) -> Source | None:
        """The source that is audible (or paused)."""
        return self._current.source if self._current is not None else None

    @property
    def gain(self) -> AppliedGain | None:
        """The ReplayGain applied to the audible source, once it's known."""
        current = self._current
        if current is None:
            return None
        with self._lock:
            return current.gain if current.gain_final else None

    def play(
        self, source: Source, *, paused: bool = False, position: float = 0.0
    ) -> None:
        """Play `source` (from `position` seconds on), then what the
        provider has after it, replacing what plays now."""
        self._errors = 0
        self._start_tries = 0
        with self._lock:
            self._failed.clear()
        self._start(source, paused=paused, position=position)

    def pause(self) -> None:
        if self._target is State.PLAYING:
            self._target = State.PAUSED
            self._cancel_tail()
            self._playbin.set_state(Gst.State.PAUSED)

    def resume(self) -> None:
        if self._target is State.PAUSED:
            self._target = State.PLAYING
            if self._seek_to is None:
                # Otherwise it plays once it has sought.
                self._playbin.set_state(Gst.State.PLAYING)
            if self._stalled:
                self._check_tail(None)

    def stop(self) -> None:
        """Stop and forget the source; the audio device is closed."""
        self._playbin.set_state(Gst.State.NULL)
        self._reset()
        self._target = State.STOPPED
        self._set_state(State.STOPPED)

    def seek(self, seconds: float) -> None:
        """Go to `seconds` into the audible source."""
        if self._target is State.STOPPED:
            return
        seconds = max(0.0, seconds)
        if self._seek_to is not None:
            # It's opened again already: seek there instead.
            self._seek_to = seconds
            return
        with self._lock:
            ahead = any(e.started for e in self._entries if e is not self._current)
        if self._current is None or ahead:
            self._reopen(seconds)
            return
        self._playbin.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            round(seconds * Gst.SECOND),
        )

    def next_changed(self) -> None:
        """The provider's next source after one that is playing or queued
        may have changed: follow it (see the module's docstring)."""
        upcoming = self.upcoming
        if upcoming is None or self._target is State.STOPPED:
            return
        candidate: Source | None = None
        # The provider is asked without the lock; if a stream started or
        # something was queued meanwhile, look again.
        for _ in range(3):
            with self._lock:
                chain = list(self._entries)
                exhausted = self._exhausted
            if not chain:
                return
            stale_from = len(chain)
            previous = chain[0].source
            for n, entry in enumerate(chain[1:], 1):
                if entry.failed:
                    # It never starts, and the provider's answers skip it:
                    # what follows it follows the one before.
                    continue
                if self._next_after(previous) is not entry.source:
                    stale_from = n
                    break
                previous = entry.source
            # What follows the last entry that stays is queued late when
            # nothing else will queue it: `about-to-finish` has found
            # nothing, or the last entry failed and never starts.
            late = (
                stale_from == len(chain)
                and (exhausted or chain[-1].failed)
                and self._errors < MAX_SKIPS
            )
            candidate = self._next_after(previous) if late else None
            with self._lock:
                if self._entries != chain:
                    continue
                for n, entry in enumerate(chain[1:], 1):
                    if n >= stale_from:
                        entry.stale = True
                    elif not entry.dropped:
                        entry.stale = False
            break
        else:
            log.warning("the queue kept changing while following the provider")
            return
        if candidate is not None:
            self._queue_late(candidate)

    def position(self) -> float | None:
        """Seconds into the audible source."""
        if self._seek_to is not None:
            return self._seek_to
        ok, position = self._playbin.query_position(Gst.Format.TIME)
        return position / Gst.SECOND if ok else None

    def duration(self) -> float | None:
        ok, duration = self._playbin.query_duration(Gst.Format.TIME)
        return duration / Gst.SECOND if ok else None

    @property
    def volume(self) -> float:
        """Linear, 0 to `MAX_VOLUME` (the sink's stream volume where it has
        one); above 1 it amplifies."""
        return float(self._playbin.get_property("volume"))

    @volume.setter
    def volume(self, value: float) -> None:
        self._playbin.set_property("volume", min(max(value, 0.0), MAX_VOLUME))

    def set_sink(self, sink: Any) -> None:
        """Play through another sink: an element, a description (see
        `make_sink`) or None for autoaudiosink's choice. What plays goes
        on where it was."""
        if isinstance(sink, str):
            sink = make_sink(sink)
        reopen = None
        if self._target is not State.STOPPED:
            with self._lock:
                first = self._entries[0] if self._entries else None
            entry = self._current or first
            if entry is not None:
                position = self.position() or 0.0
                reopen = (entry, position)
        # Only in NULL (or READY) does playbin take another sink; NULL
        # closes the device.
        self._playbin.set_state(Gst.State.NULL)
        self._sink = sink
        self._playbin.set_property("audio-sink", sink)
        if reopen is None:
            return
        entry, position = reopen
        self._start(
            entry.source,
            paused=self._target is State.PAUSED,
            position=position,
            restart=entry.reported or entry.restart,
        )

    def sink_name(self) -> str | None:
        """The factory of the sink in use, such as what autoaudiosink
        picked (pulsesink, pipewiresink; fakesink when it found none)."""
        iterator = self._playbin.iterate_recurse()
        while True:
            result, element = iterator.next()
            if result == Gst.IteratorResult.RESYNC:
                iterator.resync()
                continue
            if result != Gst.IteratorResult.OK:
                return None
            factory = element.get_factory()
            if factory is None or isinstance(element, Gst.Bin):
                continue
            klass = factory.get_metadata(Gst.ELEMENT_METADATA_KLASS) or ""
            if "Sink" in klass.split("/"):
                return str(factory.get_name())

    # The event loop's internals

    def _start(
        self,
        source: Source,
        *,
        paused: bool = False,
        position: float = 0.0,
        restart: bool = False,
    ) -> None:
        # READY stops the streaming threads; never while holding the lock,
        # which they take.
        self._playbin.set_state(Gst.State.READY)
        # What's left on the bus is about what played before.
        while self._bus.pop() is not None:
            pass
        self._reset()
        with self._lock:
            uri = self._enqueue(source, restart)
        self._target = State.PAUSED if paused else State.PLAYING
        self._seek_to = position if position > 0 else None
        self._playbin.set_property("uri", uri)
        # A seek needs the pipeline paused first.
        prerolled = paused or self._seek_to is not None
        self._playbin.set_state(Gst.State.PAUSED if prerolled else Gst.State.PLAYING)
        if self._loop is not None:
            self._start_timer = self._loop.call_later(
                START_TIMEOUT, self._start_stuck, source, paused, position, restart
            )

    def _reset(self) -> None:
        """Forget what played; the pipeline is in READY or NULL."""
        with self._lock:
            self._entries.clear()
            self._exhausted = False
            self._eos = False
            self._dropping = False
            self._settled = False
            self._deferred = False
        self._current = None
        self._seek_to = None
        self._stalled = False
        self._cancel_tail()
        self._cancel_start()

    def _reopen(self, position: float) -> None:
        """Open the audible source (or the one starting) again at
        `position`."""
        with self._lock:
            first = self._entries[0] if self._entries else None
        entry = self._current or first
        if entry is None:
            return
        self._start(
            entry.source,
            paused=self._target is State.PAUSED,
            position=position,
            restart=entry.reported or entry.restart,
        )

    def _queue_late(self, source: Source) -> bool:
        """Queue `source` after the last entry from the event loop, when
        `about-to-finish` has passed; GStreamer takes it until the end of
        the stream has passed the filter."""
        with self._lock:
            if self._eos:
                return False
            if not self._settled:
                # Once it has, the provider is asked again.
                self._deferred = True
                return True
            uri = self._enqueue(source)
            self._exhausted = False
        self._playbin.set_property("uri", uri)
        return True

    def _switch(self, after: Source | None) -> None:
        """Play what the provider has after `after`, or finish (also when
        too many failed in a row: the last error said playback stops)."""
        candidate = None
        if after is not None and self._errors < MAX_SKIPS:
            candidate = self._next_after(after)
        if candidate is not None:
            self._start(candidate)
            return
        self.stop()
        self._finished()

    def _finished(self) -> None:
        if self.on_finished is not None:
            self.on_finished()

    def _next_after(self, source: Source) -> Source | None:
        """The provider's source after `source`, past those that failed.

        Called from the streaming threads too, without the lock held.
        """
        upcoming = self.upcoming
        if upcoming is None:
            return None
        with self._lock:
            failed = list(self._failed)
        candidate = upcoming.next_source(source)
        # A repeated playlist comes round again: at most once past each.
        for _ in range(len(failed)):
            if candidate is None or not any(candidate is f for f in failed):
                break
            candidate = upcoming.next_source(candidate)
        return candidate

    def _tail(self) -> Source | None:
        """The last source that stays: what follows it plays next. One
        that failed only when no other stays (the provider may no longer
        have it, and what follows it follows the one before)."""
        with self._lock:
            staying = [e for e in self._entries if not e.stale]
        for entry in reversed(staying):
            if not entry.failed:
                return entry.source
        return staying[-1].source if staying else None

    def _set_state(self, state: State) -> None:
        if state is not self._state:
            self._state = state
            if self.on_state_changed is not None:
                self.on_state_changed(state)

    def _report(self, entry: _Entry | None) -> None:
        """Report `entry` as started once it's audible and its gain known."""
        if entry is None or entry is not self._current or entry.reported:
            return
        with self._lock:
            gain = entry.gain if entry.gain_final else None
        if gain is None:
            return
        entry.reported = True
        if not entry.restart and self.on_started is not None:
            self.on_started(entry.source, gain)

    def _fail(self, error: PlaybackError) -> None:
        if self.on_error is not None:
            self.on_error(error)

    def _read_bus(self) -> None:
        while (message := self._bus.pop()) is not None:
            # A failing callback mustn't leave the other messages unread.
            try:
                self._handle(message)
            except Exception as error:
                self._loop.call_exception_handler(
                    {"message": "Exception handling a bus message", "exception": error}
                )

    def _handle(self, message: Any) -> None:
        kind = message.type
        if kind == Gst.MessageType.STREAM_START:
            self._stream_audible(message)
        elif kind == Gst.MessageType.STATE_CHANGED:
            if message.src == self._playbin:
                _, new, pending = message.parse_state_changed()
                if pending == Gst.State.VOID_PENDING:
                    self._state_reached(new)
        elif kind == Gst.MessageType.APPLICATION:
            structure = message.get_structure()
            if structure is not None and structure.get_name() == _GAIN_MESSAGE:
                self._report(self._current)
        elif kind == Gst.MessageType.EOS:
            if self._target is not State.STOPPED:
                self._switch(self._tail())
        elif kind == Gst.MessageType.ERROR:
            self._error(message)
        elif kind == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            log.warning("%s (%s)", warning.message, debug)

    def _stream_audible(self, message: Any) -> None:
        ok, group = message.parse_group_id()
        entry = None
        with self._lock:
            for n, candidate in enumerate(self._entries):
                if ok and candidate.started and candidate.group == group:
                    entry = candidate
                    # Those before it played, or were flushed.
                    del self._entries[:n]
                    break
        if entry is None:
            log.warning("a stream started that wasn't queued")
            return
        if entry.stale or entry.dropped:
            # The provider changed its mind after the stream had passed
            # the filter.
            previous = self._current
            self._switch(previous.source if previous is not None else None)
            return
        self._current = entry
        if entry.failed:
            # What passed the filter before it failed: heard, but it was
            # reported as skipped, and the engine is still stalled.
            entry.reported = True
            return
        if self._stalled:
            # What followed the failed file started.
            self._stalled = False
            self._cancel_tail()
        self._errors = 0
        self._report(entry)

    def _state_reached(self, state: Any) -> None:
        if state == Gst.State.PLAYING:
            if self._target is State.PLAYING:
                self._settle()
                self._set_state(State.PLAYING)
        elif state == Gst.State.PAUSED:
            if self._seek_to is not None:
                position, self._seek_to = self._seek_to, None
                self._playbin.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                    round(position * Gst.SECOND),
                )
                if self._target is State.PLAYING:
                    self._playbin.set_state(Gst.State.PLAYING)
                    return
            if self._target is State.PAUSED:
                self._settle()
                self._set_state(State.PAUSED)

    def _settle(self) -> None:
        """The pipeline has started: queue what `about-to-finish` left."""
        self._cancel_start()
        self._start_tries = 0
        with self._lock:
            deferred = self._deferred
            self._settled, self._deferred = True, False
            last = self._entries[-1] if self._entries else None
        if not deferred or last is None:
            return
        candidate = self._next_after(last.source)
        if candidate is None or not self._queue_late(candidate):
            with self._lock:
                self._exhausted = True

    def _error(self, message: Any) -> None:
        error, debug = message.parse_error()
        log.debug("%s: %s", error.message, debug)
        if self._target is State.STOPPED:
            return
        entry = self._error_entry(message.src)
        problem = _problem(error)
        if entry is None:
            self.stop()
            self._fail(PlaybackError(None, problem, error.message))
            self._finished()
            return
        if entry.failed:
            # GStreamer follows the first message with more general ones.
            return
        entry.failed = True
        self._errors += 1
        with self._lock:
            self._failed.append(entry.source)
            first = self._entries[0] if self._entries else None
        candidate = None
        if self._errors < MAX_SKIPS:
            candidate = self._next_after(entry.source)
        # Nothing to play instead: playback ends.
        ended = False
        result, reached, _ = self._playbin.get_state(0)
        if result == Gst.StateChangeReturn.FAILURE and reached < Gst.State.PAUSED:
            # The pipeline failed to start (the first file failed, or one
            # after it while it started): it can't be started again. (A
            # file that fails later fails the state change of its own
            # source, which leaves the result FAILURE while it plays on.)
            position = self._seek_to or 0.0
            paused = self._target is State.PAUSED
            self._rebuild()
            if entry is not first and first is not None:
                # Opened again, the first goes on with what follows the
                # failed one.
                self._start(
                    first.source,
                    paused=paused,
                    position=position,
                    restart=first.reported or first.restart,
                )
            elif candidate is not None:
                self._start(candidate, paused=paused)
            else:
                ended = True
        elif entry is not first:
            # The next file, opened while the one before plays (or is about
            # to, a short one may be read before it's heard): that one
            # plays to its end, and so does what of the failed one passed
            # the filter already. GStreamer waits for the failed stream, so
            # no end-of-stream comes unless something takes its place.
            with self._lock:
                n = next((n for n, e in enumerate(self._entries) if e is entry), None)
                # What follows it may be queued already (a file that fails
                # once it has been read, in the decoder), or have started
                # (the stream after it dropped it from the entries).
                later = self._entries[n + 1 :] if n is not None else None
                if candidate is None:
                    # Too many failed: what was queued after it doesn't play.
                    for e in later or ():
                        e.stale = True
            if later == [] and (candidate is None or not self._queue_late(candidate)):
                with self._lock:
                    self._exhausted = True
            self._stalled = True
            if self._target is State.PLAYING and self._tail_timer is None:
                self._check_tail(None)
        elif candidate is not None:
            self._start(candidate)
        else:
            ended = True
        if ended:
            self.stop()
        self._fail(PlaybackError(entry.source, problem, error.message, candidate))
        if ended:
            self._finished()

    def _error_entry(self, element: Any) -> _Entry | None:
        """The entry an error is about.

        The element that failed says which file it was reading where it
        knows: a `filesrc` by its location, a `urisourcebin` by its URI.
        Otherwise (a decoder, say) it's the file whose stream came out of
        the decoder last: the latest that started at the filter, or
        before any did, the first one.
        """
        with self._lock:
            entries = list(self._entries)
        while element is not None and element is not self._playbin:
            uri = None
            if element.find_property("location") is not None:
                location = element.get_property("location")
                uri = Gst.filename_to_uri(location) if location else None
            elif _factory_name(element) == "urisourcebin":
                uri = element.get_property("uri")
            if uri:
                key = stream_key(uri)
                # The latest: the same file may be queued twice in a row.
                for candidate in reversed(entries):
                    if candidate.key == key and not candidate.started:
                        return candidate
                for candidate in reversed(entries):
                    if candidate.key == key:
                        return candidate
            element = element.get_parent()
        for candidate in reversed(entries):
            if candidate.started:
                return candidate
        return entries[0] if entries else None

    def _check_tail(self, last: float | None) -> None:
        """While stalled: once the audible source has ended (its position
        stops moving), start what was queued after the failed file, if
        GStreamer didn't, or what follows."""
        self._tail_timer = None
        if not self._stalled or self._target is not State.PLAYING:
            return
        position, duration = self.position(), self.duration()
        if position is None or duration is None:
            self._tail_timer = self._loop.call_later(1.0, self._check_tail, None)
            return
        if position != last:
            delay = max(duration - position, 0.0) + _TAIL_CHECK
            self._tail_timer = self._loop.call_later(delay, self._check_tail, position)
            return
        self._stalled = False
        with self._lock:
            waiting = [
                e
                for e in self._entries
                if e is not self._current and not (e.started or e.failed or e.stale)
            ]
        if waiting:
            self._start(waiting[0].source)
        else:
            self._switch(self._tail())

    def _start_stuck(
        self, source: Source, paused: bool, position: float, restart: bool
    ) -> None:
        """The pipeline hasn't started (see `START_TIMEOUT`)."""
        self._start_timer = None
        self._start_tries += 1
        log.warning("GStreamer didn't start playing %s", source.path)
        if self._start_tries < _START_TRIES:
            # Its stream may have been reported before it got stuck.
            restart = restart or (self._current is not None and self._current.reported)
            self._rebuild()
            self._start(source, paused=paused, position=position, restart=restart)
            return
        self._start_tries = 0
        self.stop()
        self._fail(PlaybackError(source, Problem.OTHER, "GStreamer didn't start"))
        self._finished()

    def _cancel_start(self) -> None:
        if self._start_timer is not None:
            self._start_timer.cancel()
            self._start_timer = None

    def _cancel_tail(self) -> None:
        if self._tail_timer is not None:
            self._tail_timer.cancel()
            self._tail_timer = None

    # Streaming threads

    def _enqueue(self, source: Source, restart: bool = False) -> str:
        """Add `source` as the next stream; under the lock."""
        uri = str(Gst.filename_to_uri(str(source.path)))
        self._entries.append(_Entry(source, stream_key(uri), restart))
        return uri

    def _about_to_finish(self, playbin: Any) -> None:
        with self._lock:
            if self._eos or not self._entries:
                return
            if not self._settled:
                self._deferred = True
                return
            last = self._entries[-1]
            if last.stale or last.failed:
                return
        candidate = self._next_after(last.source)
        with self._lock:
            if self._eos or not self._entries or self._entries[-1] is not last:
                return
            if last.stale:
                return
            if candidate is None:
                self._exhausted = True
                return
            uri = self._enqueue(candidate)
        playbin.set_property("uri", uri)

    def _element_setup(self, playbin: Any, element: Any) -> None:
        configure_sink(element, self.buffer_time, self.latency_time)

    def _probe(self, pad: Any, info: Any) -> Any:
        event = info.get_event()
        kind = event.type
        if kind == Gst.EventType.STREAM_START:
            return self._stream_started(pad, event)
        if kind == Gst.EventType.FLUSH_STOP:
            # A seek in the audible source: its end is to come again.
            with self._lock:
                self._eos = False
            return Gst.PadProbeReturn.OK
        if kind == Gst.EventType.FLUSH_START:
            return Gst.PadProbeReturn.OK
        if self._dropping:
            return Gst.PadProbeReturn.DROP
        if kind == Gst.EventType.TAG:
            if self._awaiting is not None:
                replaygain = _tags_replaygain(event.parse_tag())
                if not replaygain.empty:
                    entry, mode = self._awaiting
                    self._awaiting = None
                    applied = apply(
                        replaygain, mode, self.preamp, self.fallback, Origin.TAGS
                    )
                    self._set_gain(entry, applied, announce=True)
            # roquette takes metadata from beets; the sink would only post
            # each tag event (bitrates, several a second) as a message.
            return Gst.PadProbeReturn.DROP
        if kind == Gst.EventType.EOS:
            with self._lock:
                self._eos = True
        return Gst.PadProbeReturn.OK

    def _stream_started(self, pad: Any, event: Any) -> Any:
        key = event.parse_stream_start().partition("/")[0]
        ok, group = event.parse_group_id()
        drop = send_eos = False
        with self._lock:
            waiting = [n for n, e in enumerate(self._entries) if not e.started]
            matching = [n for n in waiting if self._entries[n].key == key]
            if matching:
                n = matching[0]
            elif waiting:
                # Stream-ids that aren't the URI's hash: the order the URIs
                # were set in still says which file it is.
                log.warning("stream %s isn't one of the queued URIs", key)
                n = waiting[0]
            entry = self._entries[n] if waiting else None
            if entry is not None:
                # Those before it that never started were flushed or failed.
                self._entries[:n] = [e for e in self._entries[:n] if e.started]
                entry.started = True
                entry.group = group if ok else None
                if self._eos or entry.stale:
                    # Queued late, after the end; or no longer next.
                    entry.stale = entry.dropped = drop = True
                    send_eos = not self._eos
                    self._eos = True
            self._dropping = drop
        if entry is None:
            log.warning("unknown stream %s", key)
        if drop:
            if send_eos:
                # The audible source plays to its end, then the bus says so.
                self._gain_src.push_event(Gst.Event.new_eos())
            pad.add_probe(
                Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
                self._drop_buffer,
            )
            return Gst.PadProbeReturn.DROP
        source = entry.source if entry is not None else None
        mode = source.mode if source is not None else Mode.TRACK
        if source is not None and source.replaygain and not source.replaygain.empty:
            self._awaiting = None
            self._set_gain(
                entry, apply(source.replaygain, mode, self.preamp, self.fallback)
            )
            return Gst.PadProbeReturn.OK
        # The fallback gain until the tags arrive; they come before the
        # first sample in the formats tried (FLAC, MP3, Ogg, MP4). If one
        # doesn't, the fallback gain stays rather than changing mid-track.
        applied = apply(None, mode, self.preamp, self.fallback)
        if mode is Mode.OFF:
            self._awaiting = None
            self._set_gain(entry, applied)
            return Gst.PadProbeReturn.OK
        self._volume.set_property("volume", applied.factor)
        with self._lock:
            if entry is not None:
                entry.gain = applied
        self._awaiting = (entry, mode)
        pad.add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            self._first_buffer,
        )
        return Gst.PadProbeReturn.OK

    def _first_buffer(self, pad: Any, info: Any) -> Any:
        if self._awaiting is not None:
            entry, _ = self._awaiting
            self._awaiting = None
            # No tags came: the fallback gain stays.
            if entry is not None:
                with self._lock:
                    gain = entry.gain
                if gain is not None:
                    self._set_gain(entry, gain, announce=True)
        return Gst.PadProbeReturn.REMOVE

    def _drop_buffer(self, pad: Any, info: Any) -> Any:
        if self._dropping:
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.REMOVE

    def _set_gain(
        self, entry: _Entry | None, applied: AppliedGain, *, announce: bool = False
    ) -> None:
        """Apply `applied` from now on, as `entry`'s final gain.

        At the stream's start, that's before the sink posts STREAM_START.
        Later (from the tags), the bus may have said the stream is audible
        already: `announce` tells the event loop.
        """
        self._volume.set_property("volume", applied.factor)
        if entry is None:
            return
        with self._lock:
            entry.gain = applied
            entry.gain_final = True
        if announce:
            self._playbin.post_message(
                Gst.Message.new_application(
                    self._playbin, Gst.Structure.new_empty(_GAIN_MESSAGE)
                )
            )


def configure_sink(
    element: Any, buffer_time: float | None, latency_time: float | None
) -> bool:
    """Set the buffer and segment sizes of an audio sink that has them
    (GstAudioBaseSink: pulsesink, alsasink); whether it had them."""
    if (
        element.find_property("buffer-time") is None
        or element.find_property("latency-time") is None
    ):
        return False
    if buffer_time is not None:
        element.set_property("buffer-time", round(buffer_time * 1_000_000))
    if latency_time is not None:
        element.set_property("latency-time", round(latency_time * 1_000_000))
    return True


def _problem(error: Any) -> Problem:
    resource, stream = Gst.ResourceError, Gst.StreamError
    if error.matches(resource.quark(), resource.NOT_FOUND):
        return Problem.MISSING
    if error.domain == GLib.quark_to_string(resource.quark()):
        return Problem.UNREADABLE
    if error.matches(stream.quark(), stream.DECODE):
        return Problem.DECODE
    for code in (
        stream.TYPE_NOT_FOUND,
        stream.WRONG_TYPE,
        stream.CODEC_NOT_FOUND,
        stream.NOT_IMPLEMENTED,
        stream.DEMUX,
        stream.FORMAT,
    ):
        if error.matches(stream.quark(), code):
            return Problem.UNSUPPORTED
    return Problem.OTHER


def _factory_name(element: Any) -> str | None:
    factory = element.get_factory()
    return str(factory.get_name()) if factory is not None else None


def _tags_replaygain(tags: Any) -> ReplayGain:
    def value(name: str) -> float | None:
        ok, number = tags.get_double(name)
        return float(number) if ok else None

    return ReplayGain(
        value(Gst.TAG_TRACK_GAIN),
        value(Gst.TAG_TRACK_PEAK),
        value(Gst.TAG_ALBUM_GAIN),
        value(Gst.TAG_ALBUM_PEAK),
    )
