# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Audio outputs as sink descriptions."""

from typing import Any

import gi

from roquette.playback.engine import make_sink
from roquette.playback.outputs import Output, describe, list_outputs, output_of

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init([])


def test_describe() -> None:
    sink: Any = Gst.ElementFactory.make("fakesink")
    assert describe(sink) == "fakesink"
    sink.props.sync = True
    sink.props.max_lateness = 5
    assert describe(sink) == "fakesink sync=true max-lateness=5"


def test_describe_quotes_strings() -> None:
    sink: Any = Gst.ElementFactory.make("filesink")
    location = '/tmp/a b "c" \\d'
    sink.props.location = location
    description = describe(sink)
    again = make_sink(description)
    [element] = [
        e for e in _children(again) if e.get_factory().get_name() == "filesink"
    ]
    assert element.props.location == location


def _children(bin_: Any) -> list[Any]:
    if not isinstance(bin_, Gst.Bin):
        return [bin_]
    found: list[Any] = []
    iterator = bin_.iterate_elements()
    while True:
        result, element = iterator.next()
        if result != Gst.IteratorResult.OK:
            return found
        found.append(element)


class FakeDevice:
    def __init__(self, name: str, element: Any, default: bool | None) -> None:
        self.name, self.element, self.default = name, element, default

    def get_display_name(self) -> str:
        return self.name

    def create_element(self, name: str | None) -> Any:
        return self.element

    def get_properties(self) -> Any:
        if self.default is None:
            return None
        value = "true" if self.default else "false"
        return Gst.Structure.new_from_string(f"properties, is-default=(boolean){value}")


def test_output_of() -> None:
    sink: Any = Gst.ElementFactory.make("fakesink")
    sink.props.sync = True
    device = FakeDevice("Speakers", sink, True)
    assert output_of(device) == Output("Speakers", "fakesink sync=true", True)
    assert output_of(FakeDevice("Other", sink, None)) == Output(
        "Other", "fakesink sync=true", False
    )
    assert output_of(FakeDevice("Broken", None, False)) is None


def test_list_outputs() -> None:
    # Without a sound server (CI), there may be none.
    outputs = list_outputs()
    assert all(isinstance(o, Output) for o in outputs)
    assert outputs == sorted(outputs, key=lambda o: not o.default)
