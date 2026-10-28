# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The playback engine: GStreamer's `playbin3`, gapless, with ReplayGain.

Threads (docs/decisions/0002-playback.md):

- The control methods and the callbacks run on the event loop's thread.
  GStreamer's bus is watched through its file descriptor on the asyncio
  loop, so its messages are handled there without a GLib main loop or a
  thread of their own.
- `about-to-finish` is emitted on a streaming thread once the current
  file has been read, well before it ends. The next file's URI is set
  right there, from the list the engine holds, under a lock and without
  waiting for the event loop.
- The gain is applied by a `volume` element in the audio filter, working
  on 32-bit floats. A pad probe on it sees the events (not the buffers)
  of each stream on its streaming thread and sets the gain when a stream
  starts, before its first sample. A stream's id starts with the SHA-256
  of its URI, which says which file it is; its group id, which the bus's
  STREAM_START message repeats when the stream reaches the sink, says
  when it becomes audible.
"""

import hashlib
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from roquette.playback.gain import Mode, ReplayGain, factor  # noqa: E402
from roquette.playback.source import Source, State  # noqa: E402

__all__ = ["Engine", "Source", "State"]

log = logging.getLogger(__name__)

# GstPlayFlags: audio only, with a software volume where the sink has none.
_PLAY_FLAGS = 0x02 | 0x10

# The gain is applied to floats: on integer samples `volume` multiplies in
# fixed point, without dither.
_FILTER = "audioconvert ! audio/x-raw,format=F32LE ! volume name=replaygain"


@dataclass(frozen=True, slots=True)
class _Queued:
    key: str
    index: int
    source: Source


def stream_key(uri: str) -> str:
    """The start of the stream-id GStreamer gives a stream read from `uri`."""
    return hashlib.sha256(uri.encode()).hexdigest()


class Engine:
    """Plays a list of sources, gapless.

    Blocking to create (GStreamer's registry is read, plugins are loaded),
    so for a worker thread; `attach` then hands it to the event loop.
    """

    def __init__(
        self,
        sink: Any = None,
        *,
        preamp: float = 0.0,
        fallback: float = 0.0,
    ) -> None:
        Gst.init(None)
        self.preamp = preamp
        self.fallback = fallback
        self.on_started: Callable[[int, Source], None] | None = None
        self.on_state_changed: Callable[[State], None] | None = None
        self.on_finished: Callable[[], None] | None = None
        self.on_error: Callable[[Source | None, str], None] | None = None

        playbin = Gst.ElementFactory.make("playbin3", "player")
        if playbin is None:
            raise RuntimeError("GStreamer's playbin3 isn't available")
        playbin.props.flags = _PLAY_FLAGS
        if isinstance(sink, str):
            description = sink
            if " " in description or "!" in description:
                # A description, e.g. "fakesink sync=true".
                sink = Gst.parse_bin_from_description(description, True)
            else:
                sink = Gst.ElementFactory.make(description)
            if sink is None:
                raise RuntimeError(f"no GStreamer element {description!r}")
        if sink is not None:
            playbin.props.audio_sink = sink
        audio_filter = Gst.parse_bin_from_description(_FILTER, True)
        playbin.props.audio_filter = audio_filter
        self._playbin = playbin
        self._volume = audio_filter.get_by_name("replaygain")
        self._gain_pad = self._volume.get_static_pad("sink")
        self._gain_pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._probe)
        playbin.connect("about-to-finish", self._about_to_finish)
        self._bus = playbin.get_bus()
        self._bus_fd: int | None = None

        # Shared with the streaming threads.
        self._lock = threading.Lock()
        self._sources: tuple[Source, ...] = ()
        # The index of the last source whose URI was set.
        self._decoding = -1
        # Sources whose URI was set and whose stream hasn't started yet, in
        # order; and those that started, by group id, until they're audible.
        self._pending: list[_Queued] = []
        self._groups: dict[int, _Queued] = {}
        # Only touched by the gain probe: the mode of the stream that
        # passes it, while its gain is still to come from its tags.
        self._awaiting_tags: Mode | None = None

        # The event loop's.
        self._state = State.STOPPED
        self._current: _Queued | None = None

    # Event loop

    def attach(self, loop: Any) -> None:
        """Handle the bus's messages on `loop` (an asyncio event loop)."""
        self._bus_fd = self._bus.get_pollfd().fd
        loop.add_reader(self._bus_fd, self._read_bus)
        self._loop = loop

    def close(self) -> None:
        self._playbin.set_state(Gst.State.NULL)
        if self._bus_fd is not None:
            self._loop.remove_reader(self._bus_fd)
            self._bus_fd = None

    # Control, on the event loop's thread

    @property
    def state(self) -> State:
        return self._state

    @property
    def current(self) -> tuple[int, Source] | None:
        """The source that is audible (or paused), and its index."""
        if self._current is None:
            return None
        return self._current.index, self._current.source

    @property
    def sources(self) -> tuple[Source, ...]:
        return self._sources

    def play(
        self, sources: Sequence[Source], index: int = 0, *, paused: bool = False
    ) -> None:
        """Play `sources` from `index` on, replacing what plays now."""
        if not 0 <= index < len(sources):
            raise IndexError(index)
        # READY stops the streaming threads; never while holding the lock,
        # which they take.
        self._playbin.set_state(Gst.State.READY)
        with self._lock:
            self._sources = tuple(sources)
            self._pending.clear()
            self._groups.clear()
            uri = self._enqueue(index)
        self._current = None
        self._playbin.props.uri = uri
        self._playbin.set_state(Gst.State.PAUSED if paused else Gst.State.PLAYING)

    def pause(self) -> None:
        if self._state is State.PLAYING:
            self._playbin.set_state(Gst.State.PAUSED)

    def resume(self) -> None:
        if self._state is State.PAUSED:
            self._playbin.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        # NULL closes the audio device.
        self._playbin.set_state(Gst.State.NULL)
        with self._lock:
            self._pending.clear()
            self._groups.clear()
        self._current = None
        self._set_state(State.STOPPED)

    def skip(self, offset: int) -> bool:
        """Play the source `offset` places from the current one, if any."""
        if self._current is None:
            return False
        index = self._current.index + offset
        if not 0 <= index < len(self._sources):
            return False
        self.play(self._sources, index, paused=self._state is State.PAUSED)
        return True

    def seek(self, seconds: float) -> None:
        self._playbin.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            max(0, round(seconds * Gst.SECOND)),
        )

    def position(self) -> float | None:
        """Seconds into the audible source."""
        ok, position = self._playbin.query_position(Gst.Format.TIME)
        return position / Gst.SECOND if ok else None

    def duration(self) -> float | None:
        ok, duration = self._playbin.query_duration(Gst.Format.TIME)
        return duration / Gst.SECOND if ok else None

    @property
    def volume(self) -> float:
        """Linear, 0–1 (the sink's stream volume where it has one)."""
        return float(self._playbin.props.volume)

    @volume.setter
    def volume(self, value: float) -> None:
        self._playbin.props.volume = min(max(value, 0.0), 1.0)

    @property
    def gain(self) -> float:
        """The ReplayGain factor applied now (to the stream at the filter)."""
        return float(self._volume.props.volume)

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

    def _set_state(self, state: State) -> None:
        if state is not self._state:
            self._state = state
            if self.on_state_changed is not None:
                self.on_state_changed(state)

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
            ok, group = message.parse_group_id()
            with self._lock:
                queued = self._groups.pop(group, None) if ok else None
                # Groups that started before this one and never reached the
                # sink were flushed.
                for stale in [g for g in self._groups if g < group]:
                    del self._groups[stale]
            if queued is None:
                log.warning("a stream started that wasn't queued")
                return
            self._current = queued
            if self.on_started is not None:
                self.on_started(queued.index, queued.source)
        elif kind == Gst.MessageType.STATE_CHANGED:
            if message.src != self._playbin:
                return
            _, new, pending = message.parse_state_changed()
            if pending == Gst.State.VOID_PENDING:
                if new == Gst.State.PLAYING:
                    self._set_state(State.PLAYING)
                elif new == Gst.State.PAUSED:
                    self._set_state(State.PAUSED)
        elif kind == Gst.MessageType.EOS:
            self.stop()
            if self.on_finished is not None:
                self.on_finished()
        elif kind == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            log.debug("%s", debug)
            # Before anything is audible, the error is the first file's.
            with self._lock:
                first = self._pending[0].source if self._pending else None
            source = self._current.source if self._current else first
            self.stop()
            if self.on_error is not None:
                self.on_error(source, error.message)
        elif kind == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            log.warning("%s (%s)", warning.message, debug)

    # Streaming threads

    def _enqueue(self, index: int) -> str:
        """Note the source at `index` as the next stream; under the lock."""
        source = self._sources[index]
        uri = Gst.filename_to_uri(str(source.path))
        self._pending.append(_Queued(stream_key(uri), index, source))
        self._decoding = index
        return str(uri)

    def _about_to_finish(self, playbin: Any) -> None:
        with self._lock:
            index = self._decoding + 1
            if index >= len(self._sources):
                return
            uri = self._enqueue(index)
        playbin.props.uri = uri

    def _probe(self, pad: Any, info: Any) -> Any:
        event = info.get_event()
        kind = event.type
        if kind == Gst.EventType.STREAM_START:
            self._stream_started(pad, event)
        elif kind == Gst.EventType.TAG:
            if self._awaiting_tags is not None:
                replaygain = _tags_replaygain(event.parse_tag())
                if not replaygain.empty:
                    self._set_gain(replaygain, self._awaiting_tags)
                    self._awaiting_tags = None
            # roquette takes metadata from beets; the sink would only post
            # each tag event (bitrates, several a second) as a message.
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    def _stream_started(self, pad: Any, event: Any) -> None:
        key = event.parse_stream_start().partition("/")[0]
        ok, group = event.parse_group_id()
        with self._lock:
            queued = None
            for n, pending in enumerate(self._pending):
                if pending.key == key:
                    queued = pending
                    # Earlier ones were flushed before they started.
                    del self._pending[: n + 1]
                    break
            if queued is None and self._pending:
                # Stream-ids that aren't the URI's hash: the order the URIs
                # were set in still says which file it is.
                log.warning("stream %s isn't one of the queued URIs", key)
                queued = self._pending.pop(0)
            if queued is not None and ok:
                self._groups[group] = queued
        if queued is None:
            log.warning("unknown stream %s", key)
        source = queued.source if queued is not None else None
        if source is not None and source.replaygain and not source.replaygain.empty:
            self._awaiting_tags = None
            self._set_gain(source.replaygain, source.mode)
            return
        # The fallback gain until the tags arrive; they come before the
        # first sample in the formats tried (FLAC, MP3, Ogg, MP4). If one
        # doesn't, the fallback gain stays rather than changing mid-track.
        mode = source.mode if source is not None else Mode.TRACK
        self._set_gain(None, mode)
        self._awaiting_tags = None if mode is Mode.OFF else mode
        if self._awaiting_tags is not None:
            pad.add_probe(
                Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
                self._first_buffer,
            )

    def _first_buffer(self, pad: Any, info: Any) -> Any:
        self._awaiting_tags = None
        return Gst.PadProbeReturn.REMOVE

    def _set_gain(self, replaygain: ReplayGain | None, mode: Mode) -> None:
        self._volume.props.volume = factor(replaygain, mode, self.preamp, self.fallback)


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
