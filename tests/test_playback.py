# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The playback engine with real GStreamer, sample-exactly.

Generated FLAC files of pseudo-random samples are played into an appsink
as fast as it takes them. The output must be every input sample, in
order, with no gap and no overlap at the transitions, each multiplied by
its file's gain: a gain that changes a sample early or late shows as a
mismatch at the boundary.
"""

import asyncio
import random
from array import array
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import gi
import pytest

from roquette.playback.engine import Engine, Source, State
from roquette.playback.gain import Mode, ReplayGain, factor

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

RATE = 44100
CHANNELS = 2
# 32-bit floats carry a 16-bit sample times a gain exactly enough.
TOLERANCE = 1e-6


class Audio:
    """A FLAC file and the samples in it, as floats."""

    def __init__(self, path: Path, samples: array[int]) -> None:
        self.path = path
        self.samples = [s / 32768 for s in samples]

    @property
    def frames(self) -> int:
        return len(self.samples) // CHANNELS


def make_flac(
    path: Path, frames: int, seed: int, tags: ReplayGain | None = None
) -> Audio:
    rnd = random.Random(seed)
    samples = array("h", (rnd.randint(-8000, 8000) for _ in range(frames * CHANNELS)))
    pipeline = Gst.parse_launch(
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
    return Audio(path, samples)


class Capture:
    """An appsink sink taking 32-bit floats, as fast as they come."""

    def __init__(self) -> None:
        self.sink = Gst.parse_bin_from_description(
            "audioconvert ! audio/x-raw,format=F32LE ! appsink name=out sync=false"
            " emit-signals=true",
            True,
        )
        self.data = bytearray()
        self.sink.get_by_name("out").connect("new-sample", self._sample)

    def _sample(self, appsink: Any) -> Any:
        buffer = appsink.emit("pull-sample").get_buffer()
        self.data += buffer.extract_dup(0, buffer.get_size())
        return Gst.FlowReturn.OK

    @property
    def samples(self) -> array[float]:
        return array("f", bytes(self.data))


def play(
    sources: Sequence[Source], *, preamp: float = 0.0, fallback: float = 0.0
) -> tuple[array[float], list[int]]:
    """Play to the end; the output and the indices of the sources started."""
    capture = Capture()
    started: list[int] = []

    async def main() -> None:
        loop = asyncio.get_running_loop()
        engine = Engine(capture.sink, preamp=preamp, fallback=fallback)
        engine.attach(loop)
        finished = loop.create_future()
        engine.on_started = lambda index, source: started.append(index)
        engine.on_finished = lambda: finished.set_result(None)
        engine.on_error = lambda source, error: finished.set_exception(
            AssertionError(error)
        )
        engine.play(sources)
        await asyncio.wait_for(finished, 30)
        engine.close()

    asyncio.run(main())
    return capture.samples, started


def check(output: Sequence[float], parts: Sequence[tuple[Audio, float]]) -> None:
    """`output` is each part's samples times its gain, back to back."""
    assert len(output) == sum(len(a.samples) for a, _ in parts)
    start = 0
    for n, (audio, gain) in enumerate(parts):
        for i, sample in enumerate(audio.samples):
            if abs(output[start + i] - sample * gain) > TOLERANCE:
                pytest.fail(
                    f"part {n}, frame {i // CHANNELS}: {output[start + i]}"
                    f" instead of {sample} × {gain}"
                )
        start += len(audio.samples)


def db(gain: float) -> float:
    return float(10 ** (gain / 20))


@pytest.fixture(scope="module")
def album(tmp_path_factory: pytest.TempPathFactory) -> list[Audio]:
    """Three tracks, with ReplayGain tags that differ from the database's."""
    directory = tmp_path_factory.mktemp("album")
    tags = [
        ReplayGain(-6.0, 0.25, -3.0, 0.5),
        ReplayGain(2.0, 0.25, -3.0, 0.5),
        ReplayGain(-1.0, 0.25, -3.0, 0.5),
    ]
    # Odd lengths, so a boundary is never on a round number of samples.
    lengths = [RATE * 2 + 123, RATE + 77, RATE // 2 + 1]
    return [
        make_flac(directory / f"{n}.flac", frames, n, tags[n])
        for n, frames in enumerate(lengths)
    ]


# What the beets database has for the album above.
DATABASE = [
    ReplayGain(-4.0, 0.25, -2.0, 0.5),
    ReplayGain(1.0, 0.25, -2.0, 0.5),
    ReplayGain(-7.0, 0.25, -2.0, 0.5),
]


def test_gapless_with_database_track_gain(album: list[Audio]) -> None:
    sources = [Source(a.path, DATABASE[n], Mode.TRACK) for n, a in enumerate(album)]
    output, started = play(sources)
    check(output, [(album[0], db(-4)), (album[1], db(1)), (album[2], db(-7))])
    assert started == [0, 1, 2]


def test_gapless_with_database_album_gain(album: list[Audio]) -> None:
    sources = [Source(a.path, DATABASE[n], Mode.ALBUM) for n, a in enumerate(album)]
    output, _ = play(sources)
    check(output, [(a, db(-2)) for a in album])


def test_gain_from_tags(album: list[Audio]) -> None:
    """Without values from the database, the files' tags count."""
    sources = [
        Source(album[0].path, None, Mode.TRACK),
        Source(album[1].path, ReplayGain(), Mode.TRACK),
        Source(album[2].path, None, Mode.ALBUM),
    ]
    output, _ = play(sources)
    check(output, [(album[0], db(-6)), (album[1], db(2)), (album[2], db(-3))])


def test_database_and_tags_mixed(album: list[Audio]) -> None:
    sources = [
        Source(album[0].path, None, Mode.TRACK),
        Source(album[1].path, DATABASE[1], Mode.TRACK),
        Source(album[2].path, None, Mode.TRACK),
    ]
    output, _ = play(sources)
    check(output, [(album[0], db(-6)), (album[1], db(1)), (album[2], db(-1))])


def test_off_preamp_and_fallback(tmp_path: Path, album: list[Audio]) -> None:
    untagged = make_flac(tmp_path / "untagged.flac", RATE // 3, 7)
    sources = [
        Source(album[0].path, DATABASE[0], Mode.OFF),
        Source(untagged.path, None, Mode.TRACK),
        Source(album[2].path, DATABASE[2], Mode.TRACK),
    ]
    output, _ = play(sources, preamp=3.0, fallback=-5.0)
    check(output, [(album[0], 1.0), (untagged, db(-2)), (album[2], db(-4))])


def test_clipping_prevention(album: list[Audio]) -> None:
    """The gain stops where the peak would reach full scale."""
    loud = ReplayGain(track_gain=15.0, track_peak=0.25)
    output, _ = play([Source(album[1].path, loud, Mode.TRACK)])
    check(output, [(album[1], 4.0)])


def test_the_same_file_twice(album: list[Audio]) -> None:
    """Streams of the same file are told apart by their order."""
    sources = [
        Source(album[2].path, DATABASE[2], Mode.TRACK),
        Source(album[2].path, ReplayGain(track_gain=0.0), Mode.TRACK),
    ]
    output, started = play(sources)
    check(output, [(album[2], db(-7)), (album[2], 1.0)])
    assert started == [0, 1]


def test_stream_ids_without_the_uri(
    album: list[Audio], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If stream-ids weren't the URI's hash, the order would still tell."""
    monkeypatch.setattr("roquette.playback.engine.stream_key", lambda uri: "?")
    sources = [Source(a.path, DATABASE[n]) for n, a in enumerate(album)]
    output, started = play(sources)
    check(output, [(album[0], db(-4)), (album[1], db(1)), (album[2], db(-7))])
    assert started == [0, 1, 2]


def test_names_that_need_escaping(tmp_path: Path) -> None:
    """The stream's id is the hash of the URI GStreamer makes of the path."""
    weird = tmp_path / "Björk – It's (live) #1 100% [a&b].flac"
    audio = make_flac(weird, RATE // 4, 3)
    output, started = play([Source(weird, ReplayGain(track_gain=-6.0))])
    check(output, [(audio, db(-6))])
    assert started == [0]


def test_seek_and_position(album: list[Audio]) -> None:
    capture = Capture()
    seen: dict[str, Any] = {}

    async def main() -> None:
        loop = asyncio.get_running_loop()
        engine = Engine(capture.sink)
        engine.attach(loop)
        paused = loop.create_future()
        finished = loop.create_future()

        def state_changed(state: State) -> None:
            if state is State.PAUSED and not paused.done():
                paused.set_result(None)

        engine.on_state_changed = state_changed
        engine.on_finished = lambda: finished.set_result(None)
        sources = [Source(a.path, DATABASE[n]) for n, a in enumerate(album[:2])]
        engine.play(sources, paused=True)
        await asyncio.wait_for(paused, 10)
        engine.seek(1.0)
        # A flushing seek in PAUSED prerolls again before position answers.
        for _ in range(100):
            if (position := engine.position()) is not None and position >= 1.0:
                break
            await asyncio.sleep(0.01)
        seen["position"] = engine.position()
        engine.resume()
        await asyncio.wait_for(finished, 30)
        seen["state"] = engine.state
        engine.close()

    asyncio.run(main())
    assert seen["position"] == pytest.approx(1.0)
    assert seen["state"] is State.STOPPED
    skipped = RATE * CHANNELS
    first = Audio(album[0].path, array("h"))
    first.samples = album[0].samples[skipped:]
    check(capture.samples, [(first, db(-4)), (album[1], db(1))])


def test_factor() -> None:
    gains = ReplayGain(-6.0, 0.5, -3.0, 0.9)
    assert factor(gains, Mode.OFF) == 1.0
    assert factor(gains, Mode.TRACK) == pytest.approx(db(-6))
    assert factor(gains, Mode.ALBUM) == pytest.approx(db(-3))
    assert factor(gains, Mode.TRACK, preamp=18.0) == pytest.approx(2.0)
    assert factor(ReplayGain(track_gain=-6.0), Mode.ALBUM) == pytest.approx(db(-6))
    assert factor(ReplayGain(album_gain=-3.0), Mode.TRACK) == pytest.approx(db(-3))
    assert factor(None, Mode.TRACK, fallback=-6.0) == pytest.approx(db(-6))
    assert factor(ReplayGain(), Mode.TRACK, preamp=6.0) == pytest.approx(db(6))


def test_replaygain_from_fields() -> None:
    fields = {"rg_track_gain": -6.5, "rg_track_peak": 0.98, "rg_album_gain": None}
    assert ReplayGain.from_fields(fields) == ReplayGain(-6.5, 0.98, None, None)
    assert ReplayGain.from_fields({}).empty


def test_missing_file(tmp_path: Path) -> None:
    errors: list[tuple[Source | None, str]] = []
    missing = Source(tmp_path / "missing.flac")

    async def main() -> None:
        loop = asyncio.get_running_loop()
        engine = Engine(Capture().sink)
        engine.attach(loop)
        failed = loop.create_future()

        def error(source: Source | None, message: str) -> None:
            errors.append((source, message))
            failed.set_result(None)

        engine.on_error = error
        engine.play([missing])
        await asyncio.wait_for(failed, 10)
        assert engine.state is State.STOPPED
        engine.close()

    asyncio.run(main())
    assert errors[0][0] is missing


def test_missing_next_file(tmp_path: Path, album: list[Audio]) -> None:
    errors: list[tuple[Source | None, str]] = []
    first = Source(album[2].path)
    missing = Source(tmp_path / "missing.flac")

    async def main() -> None:
        loop = asyncio.get_running_loop()
        engine = Engine(Capture().sink)
        engine.attach(loop)
        failed = loop.create_future()

        def error(source: Source | None, message: str) -> None:
            errors.append((source, message))
            if not failed.done():
                failed.set_result(None)

        engine.on_error = error
        # The next file is opened while the first still plays.
        engine.play([first, missing])
        await asyncio.wait_for(failed, 10)
        engine.close()

    asyncio.run(main())
    assert errors[0][0] is missing
