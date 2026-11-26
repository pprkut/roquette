# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The engine's control, errors, changes of the next source and outputs,
with real GStreamer.

Where it matters where a stream is in the pipeline (before the filter,
in playsink's queue, at the sink), the appsink syncs to the clock, so
the queues fill as they do with a sound card; those tests take as long
as their files play.
"""

import time
from pathlib import Path
from typing import Any

import pytest

from roquette.playback import engine as engine_module
from roquette.playback.engine import (
    MAX_SKIPS,
    MAX_VOLUME,
    Engine,
    configure_sink,
    make_sink,
)
from roquette.playback.gain import ReplayGain
from roquette.playback.source import Problem, Source, SourceList, State
from tests.audio import (
    CHANNELS,
    RATE,
    Audio,
    Capture,
    Recorder,
    check,
    compare,
    db,
    make_flac,
    play,
    run,
    until,
)


@pytest.fixture(scope="module")
def tracks(tmp_path_factory: pytest.TempPathFactory) -> list[Audio]:
    """Short tracks: 3 s, 0.6 s and 0.9 s.

    Before the first one ends, the next one is queued for about two
    seconds, and past the filter for one.
    """
    directory = tmp_path_factory.mktemp("tracks")
    lengths = [RATE * 3 + 11, RATE * 6 // 10 + 3, RATE * 9 // 10 + 5]
    return [
        make_flac(directory / f"{n}.flac", frames, 10 + n)
        for n, frames in enumerate(lengths)
    ]


GAINS = [ReplayGain(-4.0, 0.25), ReplayGain(1.0, 0.25), ReplayGain(-7.0, 0.25)]


def sources_for(tracks: list[Audio]) -> list[Source]:
    return [Source(a.path, GAINS[n]) for n, a in enumerate(tracks)]


def gain_of(n: int) -> float:
    return db(GAINS[n].track_gain or 0.0)


# Errors


def test_missing_first_file(tmp_path: Path, tracks: list[Audio]) -> None:
    missing = Source(tmp_path / "missing.flac")
    sources = [missing, sources_for(tracks)[1]]
    output, recorder = play(sources)
    check(output, [(tracks[1], gain_of(1))])
    assert recorder.indices(sources) == [1]
    [error] = recorder.errors
    assert error.source is missing
    assert error.problem is Problem.MISSING
    assert error.skipped_to is sources[1]


@pytest.mark.parametrize("sync", [False, True])
def test_missing_next_file(tmp_path: Path, tracks: list[Audio], sync: bool) -> None:
    """The file before plays to its end, the one after follows gaplessly."""
    first, last = sources_for(tracks)[0], sources_for(tracks)[2]
    sources = [first, Source(tmp_path / "missing.flac"), last]
    output, recorder = play(sources, sync=sync)
    check(output, [(tracks[0], gain_of(0)), (tracks[2], gain_of(2))])
    assert recorder.indices(sources) == [0, 2]
    [error] = recorder.errors
    assert error.source is sources[1]
    assert error.problem is Problem.MISSING
    assert error.skipped_to is last


def test_replacement_not_taken(
    tmp_path: Path, tracks: list[Audio], monkeypatch: pytest.MonkeyPatch
) -> None:
    """GStreamer 1.28 doesn't always take the file queued in place of a
    failed one: once the one before has ended, the engine starts it."""

    def queue_without_the_uri(engine: Engine, source: Source) -> bool:
        with engine._lock:
            engine._enqueue(source)
            engine._exhausted = False
        return True

    monkeypatch.setattr(Engine, "_queue_late", queue_without_the_uri)
    # Long enough for `about-to-finish` to come after the start.
    first, last = sources_for(tracks)[0], sources_for(tracks)[2]
    sources = [first, Source(tmp_path / "missing.flac"), last]
    output, recorder = play(sources, sync=True)
    check(output, [(tracks[0], gain_of(0)), (tracks[2], gain_of(2))])
    assert recorder.indices(sources) == [0, 2]
    assert [e.skipped_to for e in recorder.errors] == [last]


@pytest.mark.parametrize("sync", [False, True])
def test_missing_last_file(tmp_path: Path, tracks: list[Audio], sync: bool) -> None:
    """GStreamer waits for the stream that failed: the engine finds the
    end of the one before."""
    sources = [sources_for(tracks)[1], Source(tmp_path / "missing.flac")]
    output, recorder = play(sources, sync=sync)
    check(output, [(tracks[1], gain_of(1))])
    [error] = recorder.errors
    assert error.skipped_to is None
    assert recorder.states[-1] is State.STOPPED
    assert recorder.finished.done()


@pytest.mark.parametrize("sync", [False, True])
def test_two_failures_in_a_row(tmp_path: Path, tracks: list[Audio], sync: bool) -> None:
    sources = [
        sources_for(tracks)[1],
        Source(tmp_path / "missing.flac"),
        Source(tmp_path),
        sources_for(tracks)[2],
    ]
    output, recorder = play(sources, sync=sync)
    check(output, [(tracks[1], gain_of(1)), (tracks[2], gain_of(2))])
    assert [e.problem for e in recorder.errors] == [
        Problem.MISSING,
        Problem.UNREADABLE,
    ]
    assert [e.source for e in recorder.errors] == sources[1:3]


def test_unsupported_files(tmp_path: Path, tracks: list[Audio]) -> None:
    text = tmp_path / "text.flac"
    text.write_text("not audio\n" * 100)
    truncated = tmp_path / "truncated.flac"
    truncated.write_bytes(tracks[1].path.read_bytes()[:30])
    sources = [Source(text), Source(truncated), *sources_for(tracks)[1:2]]
    output, recorder = play(sources)
    check(output, [(tracks[1], gain_of(1))])
    assert [(e.source, e.problem) for e in recorder.errors] == [
        (sources[0], Problem.UNSUPPORTED),
        (sources[1], Problem.UNSUPPORTED),
    ]


def test_too_many_failures(tmp_path: Path) -> None:
    sources = [Source(tmp_path / f"{n}.flac") for n in range(MAX_SKIPS + 5)]
    output, recorder = play(sources)
    assert len(output) == 0
    assert len(recorder.errors) == MAX_SKIPS
    assert recorder.errors[-2].skipped_to is sources[MAX_SKIPS - 1]
    assert recorder.errors[-1].skipped_to is None
    assert recorder.started == []


def test_too_many_failures_after_the_audible_file(
    tmp_path: Path, tracks: list[Audio]
) -> None:
    """The next files fail while the one before plays: once the limit is
    reached, that one plays to its end and playback stops, although a
    file after the failed ones would play."""
    first, last = sources_for(tracks)[0], sources_for(tracks)[2]
    missing = [Source(tmp_path / f"{n}.flac") for n in range(MAX_SKIPS)]
    sources = [first, *missing, last]
    output, recorder = play(sources, sync=True)
    check(output, [(tracks[0], gain_of(0))])
    assert recorder.indices(sources) == [0]
    assert [e.source for e in recorder.errors] == missing
    assert recorder.errors[-1].skipped_to is None
    assert recorder.states[-1] is State.STOPPED


def test_next_file_fails_after_it_passed_the_filter(tracks: list[Audio]) -> None:
    """The audible file plays to its end, the failed one isn't reported
    as started, and the one after it follows."""
    from gi.repository import GLib, Gst

    sources = sources_for(tracks)
    capture = Capture(sync=True)

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        engine.upcoming = SourceList(tuple(sources))
        engine.play(sources[0])
        entries = engine._entries
        await until(
            lambda: (
                len(entries) >= 2
                and entries[0].source is sources[0]
                and entries[1].started
            )
        )
        assert engine.current is sources[0]
        # As a read error of the second file's source would be posted.
        element: Any = Gst.ElementFactory.make("filesrc")
        element.props.location = str(sources[1].path)
        resource = Gst.ResourceError
        error: Any = GLib.Error.new_literal(
            resource.quark(), "failed", int(resource.READ)
        )
        engine._bus.post(Gst.Message.new_error(element, error, "test"))
        await recorder.finished

    recorder = run(scenario, capture.sink)
    output = capture.samples
    compare(output, 0, tracks[0], gain_of(0), "the audible track")
    last = tracks[2]
    compare(output, len(output) - len(last.samples), last, gain_of(2), "the next one")
    assert recorder.indices(sources) == [0, 2]
    [error] = recorder.errors
    assert (error.source, error.skipped_to) == (sources[1], sources[2])


def test_problems() -> None:
    from gi.repository import GLib, Gst

    def error(domain: Any, code: Any) -> Any:
        return GLib.Error.new_literal(domain.quark(), "", int(code))

    problem = engine_module._problem
    resource, stream = Gst.ResourceError, Gst.StreamError
    assert problem(error(resource, resource.NOT_FOUND)) is Problem.MISSING
    assert problem(error(resource, resource.OPEN_READ)) is Problem.UNREADABLE
    assert problem(error(stream, stream.DECODE)) is Problem.DECODE
    assert problem(error(stream, stream.CODEC_NOT_FOUND)) is Problem.UNSUPPORTED
    assert problem(error(Gst.CoreError, Gst.CoreError.FAILED)) is Problem.OTHER


# The next source changes


def test_next_changed_before_it_is_queued(tracks: list[Audio]) -> None:
    sources = sources_for(tracks)
    capture = Capture()

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        upcoming = SourceList((sources[0], sources[1]))
        engine.upcoming = upcoming
        engine.play(sources[0], paused=True)
        await until(lambda: engine.state is State.PAUSED)
        upcoming.sources = (sources[0], sources[2])
        engine.next_changed()
        engine.resume()
        await recorder.finished
        assert recorder.indices(sources) == [0, 2]

    run(scenario, capture.sink)
    check(capture.samples, [(tracks[0], gain_of(0)), (tracks[2], gain_of(2))])


def test_next_changed_after_it_was_queued(tracks: list[Audio]) -> None:
    """The stream queued has not reached the filter: the audible track
    plays to its end, then the new next one."""
    sources = sources_for(tracks)
    capture = Capture(sync=True)

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        upcoming = SourceList((sources[0], sources[1]))
        engine.upcoming = upcoming
        engine.play(sources[0])
        await until(lambda: len(engine._entries) == 2)
        assert not engine._entries[1].started
        upcoming.sources = (sources[0], sources[2])
        engine.next_changed()
        await recorder.finished
        assert recorder.indices(sources) == [0, 2]

    run(scenario, capture.sink)
    check(capture.samples, [(tracks[0], gain_of(0)), (tracks[2], gain_of(2))])


def test_next_changed_after_it_passed_the_filter(tracks: list[Audio]) -> None:
    """Some of the stale stream may be heard before the new one starts."""
    sources = sources_for(tracks)
    capture = Capture(sync=True)

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        upcoming = SourceList((sources[0], sources[1]))
        engine.upcoming = upcoming
        engine.play(sources[0])
        await until(lambda: len(engine._entries) == 2 and engine._entries[1].started)
        assert engine.current is sources[0]
        upcoming.sources = (sources[0], sources[2])
        engine.next_changed()
        await recorder.finished
        assert recorder.indices(sources) == [0, 2]

    run(scenario, capture.sink)
    output = capture.samples
    first, last = tracks[0], tracks[2]
    compare(output, 0, first, gain_of(0), "the audible track")
    middle = len(output) - len(first.samples) - len(last.samples)
    assert 0 <= middle < RATE * CHANNELS // 2
    stale = Audio(tracks[1].path, tracks[1].samples[:middle])
    compare(output, len(first.samples), stale, gain_of(1), "the stale track")
    compare(output, len(output) - len(last.samples), last, gain_of(2), "the new one")


def test_next_removed_after_it_was_queued(tracks: list[Audio]) -> None:
    sources = sources_for(tracks)
    capture = Capture(sync=True)

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        upcoming = SourceList((sources[0], sources[1]))
        engine.upcoming = upcoming
        engine.play(sources[0])
        await until(lambda: len(engine._entries) == 2)
        upcoming.sources = (sources[0],)
        engine.next_changed()
        await recorder.finished
        assert recorder.indices(sources) == [0]
        assert engine.state is State.STOPPED

    run(scenario, capture.sink)
    check(capture.samples, [(tracks[0], gain_of(0))])


def test_next_changed_after_a_replacement_was_queued(
    tmp_path: Path, tracks: list[Audio]
) -> None:
    """The file queued in place of a failed one stays queued when the
    provider's answer is the same."""
    first, last = sources_for(tracks)[0], sources_for(tracks)[2]
    sources = [first, Source(tmp_path / "missing.flac"), last]
    capture = Capture(sync=True)

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        engine.upcoming = SourceList(tuple(sources))
        engine.play(first)
        await until(lambda: len(engine._entries) == 3 and engine._entries[1].failed)
        engine.next_changed()
        assert [e.stale for e in engine._entries] == [False, False, False]
        await recorder.finished

    recorder = run(scenario, capture.sink)
    check(capture.samples, [(tracks[0], gain_of(0)), (tracks[2], gain_of(2))])
    assert recorder.indices(sources) == [0, 2]


def test_changed_back_before_it_starts(tracks: list[Audio]) -> None:
    sources = sources_for(tracks)
    capture = Capture(sync=True)

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        upcoming = SourceList((sources[0], sources[1]))
        engine.upcoming = upcoming
        engine.play(sources[0])
        await until(lambda: len(engine._entries) == 2)
        upcoming.sources = (sources[0], sources[2])
        engine.next_changed()
        upcoming.sources = (sources[0], sources[1])
        engine.next_changed()
        await recorder.finished

    recorder = run(scenario, capture.sink)
    check(capture.samples, [(tracks[0], gain_of(0)), (tracks[1], gain_of(1))])
    assert recorder.indices(sources) == [0, 1]


def test_appended_while_the_last_track_plays(tracks: list[Audio]) -> None:
    """Queued late, but before the end of the stream passed the filter:
    gapless."""
    sources = sources_for(tracks)
    capture = Capture(sync=True)

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        upcoming = SourceList((sources[0],))
        engine.upcoming = upcoming
        engine.play(sources[0])
        await until(lambda: engine._exhausted)
        assert not engine._eos
        upcoming.sources = (sources[0], sources[1])
        engine.next_changed()
        await recorder.finished

    recorder = run(scenario, capture.sink)
    check(capture.samples, [(tracks[0], gain_of(0)), (tracks[1], gain_of(1))])
    assert recorder.indices(sources) == [0, 1]


def test_appended_after_the_end_passed_the_filter(tracks: list[Audio]) -> None:
    """Too late to be gapless: it starts after the end."""
    sources = sources_for(tracks)
    capture = Capture(sync=True)

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        upcoming = SourceList((sources[1],))
        engine.upcoming = upcoming
        engine.play(sources[1])
        await until(lambda: engine._eos)
        upcoming.sources = (sources[1], sources[2])
        engine.next_changed()
        await recorder.finished

    recorder = run(scenario, capture.sink)
    check(capture.samples, [(tracks[1], gain_of(1)), (tracks[2], gain_of(2))])
    assert recorder.indices(sources) == [1, 2]


# Control


def test_states(tracks: list[Audio]) -> None:
    sources = sources_for(tracks)

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        engine.play(sources[0], paused=True)
        await until(lambda: engine.state is State.PAUSED)
        assert engine.current is sources[0]
        assert engine.position() == 0.0
        engine.resume()
        await until(lambda: engine.state is State.PLAYING)
        engine.pause()
        await until(lambda: engine.state is State.PAUSED)
        # Paused before it plays.
        engine.play(sources[1])
        engine.pause()
        await until(lambda: engine.current is sources[1])
        await until(lambda: engine.state is State.PAUSED)
        engine.stop()
        assert engine.current is None
        engine.resume()
        engine.seek(1.0)
        assert engine.state is State.STOPPED

    recorder = run(scenario, Capture(sync=True).sink)
    assert recorder.states == [
        State.PAUSED,
        State.PLAYING,
        State.PAUSED,
        State.STOPPED,
    ]
    assert recorder.indices(sources) == [0, 1]


def test_play_from_a_position(tracks: list[Audio]) -> None:
    sources = sources_for(tracks)
    capture = Capture()

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        engine.upcoming = SourceList((sources[0], sources[1]))
        engine.play(sources[0], position=1.0)
        await recorder.finished

    recorder = run(scenario, capture.sink)
    check(
        capture.samples, [(tracks[0].after(1.0), gain_of(0)), (tracks[1], gain_of(1))]
    )
    assert recorder.indices(sources) == [0, 1]
    # Without flickering through PAUSED on the way.
    assert recorder.states == [State.PLAYING, State.STOPPED]


def test_seek_and_position(tracks: list[Audio]) -> None:
    sources = sources_for(tracks)
    capture = Capture()

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        engine.upcoming = SourceList((sources[0], sources[1]))
        engine.play(sources[0], paused=True)
        await until(lambda: engine.state is State.PAUSED)
        engine.seek(1.0)
        # A flushing seek in PAUSED prerolls again before position answers.
        await until(lambda: (engine.position() or 0.0) >= 1.0)
        assert engine.position() == pytest.approx(1.0)
        engine.resume()
        await recorder.finished
        assert engine.state is State.STOPPED

    run(scenario, capture.sink)
    check(
        capture.samples, [(tracks[0].after(1.0), gain_of(0)), (tracks[1], gain_of(1))]
    )


def test_seek_after_the_next_stream_passed_the_filter(tracks: list[Audio]) -> None:
    """A seek would go to the next file's stream: the audible file is
    opened again at the position instead, and not reported again."""
    sources = sources_for(tracks)
    capture = Capture(sync=True)

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        engine.upcoming = SourceList((sources[0], sources[1]))
        engine.play(sources[0])
        await until(lambda: len(engine._entries) == 2 and engine._entries[1].started)
        capture.data.clear()
        engine.seek(0.5)
        assert engine.position() == 0.5
        await recorder.finished

    recorder = run(scenario, capture.sink)
    check(
        capture.samples, [(tracks[0].after(0.5), gain_of(0)), (tracks[1], gain_of(1))]
    )
    assert recorder.indices(sources) == [0, 1]
    assert State.PAUSED not in recorder.states


def test_seek_with_the_next_stream_queued(tracks: list[Audio]) -> None:
    sources = sources_for(tracks)
    capture = Capture(sync=True)

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        engine.upcoming = SourceList((sources[0], sources[1]))
        engine.play(sources[0])
        await until(lambda: len(engine._entries) == 2)
        assert not engine._entries[1].started
        capture.data.clear()
        engine.seek(0.5)
        await recorder.finished

    recorder = run(scenario, capture.sink)
    check(
        capture.samples, [(tracks[0].after(0.5), gain_of(0)), (tracks[1], gain_of(1))]
    )
    assert recorder.indices(sources) == [0, 1]


def test_volume_above_one(tracks: list[Audio]) -> None:
    sources = sources_for(tracks)[1:2]
    capture = Capture()

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        engine.volume = 2.0
        assert engine.volume == 2.0
        engine.play(sources[0])
        await recorder.finished
        engine.volume = 100.0
        assert engine.volume == MAX_VOLUME
        engine.volume = -1.0
        assert engine.volume == 0.0

    run(scenario, capture.sink)
    check(capture.samples, [(tracks[1], gain_of(1) * 2)])


def test_gain_property(tracks: list[Audio]) -> None:
    sources = sources_for(tracks)

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        assert engine.gain is None
        engine.play(sources[0], paused=True)
        await until(lambda: engine.state is State.PAUSED)
        gain = engine.gain
        assert gain is not None
        assert gain.factor == pytest.approx(gain_of(0))
        assert recorder.started == [(sources[0], gain)]

    run(scenario, Capture().sink)


# Outputs


def test_set_sink_while_playing(tracks: list[Audio]) -> None:
    """What plays goes on through the new sink where it was."""
    sources = sources_for(tracks)
    before, after = Capture(sync=True), Capture()
    switched: list[float] = []

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        engine.upcoming = SourceList((sources[0], sources[1]))
        engine.play(sources[0])
        await until(lambda: (engine.position() or 0.0) > 0.3)
        position = engine.position()
        assert position is not None
        switched.append(position)
        engine.set_sink(after.sink)
        await recorder.finished

    recorder = run(scenario, before.sink)
    assert recorder.indices(sources) == [0, 1]
    # The new sink plays the first track from where it was (the engine
    # asks for the position a moment after the test), then the second.
    output = after.samples
    skipped = tracks[0].frames - (len(output) // CHANNELS - tracks[1].frames)
    assert 0 <= skipped / RATE - switched[0] < 0.01
    check(
        output, [(tracks[0].after(skipped / RATE), gain_of(0)), (tracks[1], gain_of(1))]
    )


def test_set_sink_while_stopped(tracks: list[Audio]) -> None:
    capture = Capture()

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        engine.set_sink(capture.sink)
        assert engine.state is State.STOPPED
        engine.play(sources_for(tracks)[1])
        await recorder.finished

    run(scenario, Capture().sink)
    check(capture.samples, [(tracks[1], gain_of(1))])


def test_make_sink() -> None:
    assert make_sink("fakesink").get_factory().get_name() == "fakesink"
    bin_ = make_sink("fakesink sync=true")
    assert bin_.get_by_name("fakesink0") is not None or bin_.numchildren == 1
    with pytest.raises(RuntimeError):
        make_sink("no-such-sink")


def test_a_start_that_gets_stuck(
    tracks: list[Audio], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pipeline that doesn't start is replaced; as the engine sees it,
    when the state it was started in isn't reached."""
    monkeypatch.setattr(engine_module, "START_TIMEOUT", 0.3)
    sources = sources_for(tracks)[1:3]
    capture = Capture()
    reached = Engine._state_reached
    calls = 0

    def miss_the_first(engine: Engine, state: object) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            reached(engine, state)

    monkeypatch.setattr(Engine, "_state_reached", miss_the_first)

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        engine.upcoming = SourceList(tuple(sources))
        engine.volume = 0.5
        playbin = engine._playbin
        engine.play(sources[0], paused=True)
        await until(lambda: engine.state is State.PAUSED)
        assert engine._playbin is not playbin
        assert engine.volume == 0.5
        engine.resume()
        await recorder.finished

    recorder = run(scenario, capture.sink)
    check(
        capture.samples, [(tracks[1], gain_of(1) * 0.5), (tracks[2], gain_of(2) * 0.5)]
    )
    assert recorder.indices(sources) == [0, 1]
    assert recorder.errors == []


def test_a_start_that_never_happens(
    tracks: list[Audio], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "START_TIMEOUT", 0.1)
    monkeypatch.setattr(Engine, "_state_reached", lambda engine, state: None)
    source = sources_for(tracks)[1]

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        start = time.monotonic()
        engine.play(source, paused=True)
        await recorder.finished
        assert engine.state is State.STOPPED
        # Each try waits longer: a start that is only slow gets time.
        assert time.monotonic() - start >= 0.1 + 0.2 + 0.3

    recorder = run(scenario, Capture().sink)
    [error] = recorder.errors
    assert (error.source, error.problem) == (source, Problem.OTHER)


def test_configure_sink() -> None:
    from gi.repository import Gst

    sink: Any = Gst.ElementFactory.make("pulsesink")
    if sink is None:
        pytest.skip("no pulsesink")
    assert configure_sink(sink, 0.4, 0.1)
    assert (sink.props.buffer_time, sink.props.latency_time) == (400_000, 100_000)
    assert configure_sink(sink, None, None)
    assert sink.props.buffer_time == 400_000
    assert not configure_sink(Gst.ElementFactory.make("fakesink"), 0.4, 0.1)


def test_sink_configured_in_the_pipeline(
    tracks: list[Audio], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every element playbin adds goes through `configure_sink`: the
    sinks autoaudiosink makes too."""
    seen: list[str] = []

    def configure(element: Any, buffer_time: object, latency_time: object) -> bool:
        seen.append(element.get_factory().get_name())
        assert (buffer_time, latency_time) == (0.3, 0.05)
        return False

    monkeypatch.setattr(engine_module, "configure_sink", configure)
    capture = Capture()

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        engine.play(sources_for(tracks)[1])
        await recorder.finished

    run(scenario, capture.sink, buffer_time=0.3, latency_time=0.05)
    assert "appsink" in seen
    assert "flacdec" in seen
