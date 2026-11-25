# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Generated audio and an engine playing into an appsink, for the tests
of the playback engine.

The files hold pseudo-random samples, so a missing, repeated or
misplaced frame, or a gain a sample early or late, shows when the
output is compared with them.
"""

import asyncio
import random
import time
from array import array
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import gi
import pytest

from roquette.playback.engine import Engine
from roquette.playback.gain import AppliedGain, ReplayGain
from roquette.playback.source import PlaybackError, Source, SourceList, State

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init([])

RATE = 44100
CHANNELS = 2
# 32-bit floats carry a 16-bit sample times a gain exactly enough.
TOLERANCE = 1e-6


class Audio:
    """A FLAC file and the samples in it, as floats."""

    def __init__(self, path: Path, samples: Sequence[float]) -> None:
        self.path = path
        self.samples = list(samples)

    @property
    def frames(self) -> int:
        return len(self.samples) // CHANNELS

    def after(self, seconds: float) -> "Audio":
        """The samples from `seconds` on."""
        start = round(seconds * RATE) * CHANNELS
        return Audio(self.path, self.samples[start:])


def make_flac(
    path: Path, frames: int, seed: int, tags: ReplayGain | None = None
) -> Audio:
    rnd = random.Random(seed)
    samples = array("h", (rnd.randint(-8000, 8000) for _ in range(frames * CHANNELS)))
    pipeline: Any = Gst.parse_launch(
        "appsrc name=src format=time"
        f" caps=audio/x-raw,format=S16LE,rate={RATE},channels={CHANNELS},"
        "layout=interleaved ! audioconvert ! flacenc name=encoder ! filesink name=out"
    )
    pipeline.get_by_name("out").props.location = str(path)
    if tags is not None:
        tag_list = Gst.TagList.new_empty()
        for name, value in [
            (Gst.TAG_TRACK_GAIN, tags.track_gain),
            (Gst.TAG_TRACK_PEAK, tags.track_peak),
            (Gst.TAG_ALBUM_GAIN, tags.album_gain),
            (Gst.TAG_ALBUM_PEAK, tags.album_peak),
        ]:
            if value is not None:
                tag_list.add_value(Gst.TagMergeMode.REPLACE, name, value)
        pipeline.get_by_name("encoder").merge_tags(tag_list, Gst.TagMergeMode.REPLACE)
    source = pipeline.get_by_name("src")
    pipeline.set_state(Gst.State.PLAYING)
    buffer = Gst.Buffer.new_wrapped(samples.tobytes())
    buffer.pts = 0
    buffer.duration = Gst.util_uint64_scale(frames, Gst.SECOND, RATE)
    source.emit("push-buffer", buffer)
    source.emit("end-of-stream")
    message = pipeline.get_bus().timed_pop_filtered(
        10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR
    )
    pipeline.set_state(Gst.State.NULL)
    assert message is not None and message.type == Gst.MessageType.EOS
    return Audio(path, [s / 32768 for s in samples])


class Capture:
    """An appsink sink taking 32-bit floats: as fast as they come, or in
    real time with `sync`, which keeps the pipeline's queues as they are
    with a sound card."""

    def __init__(self, *, sync: bool = False) -> None:
        self.sink: Any = Gst.parse_bin_from_description(
            "audioconvert ! audio/x-raw,format=F32LE ! appsink name=out"
            f" sync={'true' if sync else 'false'} emit-signals=true",
            True,
        )
        self.data = bytearray()
        appsink: Any = self.sink.get_by_name("out")
        appsink.connect("new-sample", self._sample)

    def _sample(self, appsink: Any) -> Any:
        buffer = appsink.emit("pull-sample").get_buffer()
        self.data += buffer.extract_dup(0, buffer.get_size())
        return Gst.FlowReturn.OK

    @property
    def samples(self) -> array[float]:
        return array("f", bytes(self.data))


class Recorder:
    """What an engine reported."""

    def __init__(self, engine: Engine, loop: asyncio.AbstractEventLoop) -> None:
        self.started: list[tuple[Source, AppliedGain]] = []
        self.states: list[State] = []
        self.errors: list[PlaybackError] = []
        self.finished: asyncio.Future[None] = loop.create_future()
        engine.on_started = lambda source, gain: self.started.append((source, gain))
        engine.on_state_changed = self.states.append
        engine.on_error = self.errors.append
        engine.on_finished = self._finished

    def _finished(self) -> None:
        if not self.finished.done():
            self.finished.set_result(None)

    def indices(self, sources: Sequence[Source]) -> list[int]:
        """The positions in `sources` of the sources started, in order."""
        return [
            next(n for n, s in enumerate(sources) if s is started)
            for started, _ in self.started
        ]


Scenario = Callable[[Engine, Recorder], Awaitable[None]]


def run(
    scenario: Scenario, sink: Any = None, timeout: float = 30, **options: Any
) -> Recorder:
    """Run `scenario` with an engine on its own event loop."""
    recorder: Recorder | None = None

    async def main() -> None:
        nonlocal recorder
        loop = asyncio.get_running_loop()
        engine = Engine(sink, **options)
        engine.attach(loop)
        recorder = Recorder(engine, loop)
        try:
            await asyncio.wait_for(scenario(engine, recorder), timeout)
        except TimeoutError:
            pytest.fail(f"timed out: {describe(engine, recorder)}")
        finally:
            engine.close()

    asyncio.run(main())
    assert recorder is not None
    return recorder


def describe(engine: Engine, recorder: Recorder) -> str:
    """The engine's state, for a test that got stuck."""
    entries = [
        (e.source.path.name, e.started, e.failed, e.stale, e.dropped)
        for e in engine._entries
    ]
    flags = {
        name: getattr(engine, f"_{name}")
        for name in ("target", "exhausted", "eos", "settled", "deferred", "stalled")
    }
    return (
        f"{engine.state}, entries {entries}, {flags},"
        f" position {engine.position()}, duration {engine.duration()},"
        f" errors {recorder.errors}"
    )


def play(
    sources: Sequence[Source], *, sync: bool = False, **options: Any
) -> tuple[array[float], Recorder]:
    """Play `sources` to the end; the output and what was reported."""
    capture = Capture(sync=sync)

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        engine.upcoming = SourceList(tuple(sources))
        engine.play(sources[0])
        await recorder.finished

    recorder = run(scenario, capture.sink, **options)
    return capture.samples, recorder


async def until(condition: Callable[[], object], timeout: float = 10) -> None:
    """Wait on the event loop until `condition()` is true."""
    end = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > end:
            raise TimeoutError("the condition never came true")
        await asyncio.sleep(0.005)


def check(output: Sequence[float], parts: Sequence[tuple[Audio, float]]) -> None:
    """`output` is each part's samples times its gain, back to back."""
    assert len(output) == sum(len(a.samples) for a, _ in parts), (
        f"{len(output) // CHANNELS} frames instead of {sum(a.frames for a, _ in parts)}"
    )
    start = 0
    for n, (audio, gain) in enumerate(parts):
        compare(output, start, audio, gain, f"part {n}")
        start += len(audio.samples)


def compare(
    output: Sequence[float], start: int, audio: Audio, gain: float, what: str = ""
) -> None:
    """`audio` times `gain` is in `output` from sample `start` on."""
    for i, sample in enumerate(audio.samples):
        if abs(output[start + i] - sample * gain) > TOLERANCE:
            pytest.fail(
                f"{what}, frame {i // CHANNELS}: {output[start + i]}"
                f" instead of {sample} × {gain}"
            )


def db(gain: float) -> float:
    return float(10 ** (gain / 20))
