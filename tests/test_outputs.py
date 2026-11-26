# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Audio outputs as sink descriptions."""

from typing import Any

import gi

from roquette.playback.engine import make_sink
from roquette.playback.outputs import (
    Output,
    describe,
    list_outputs,
    name_target,
    output_of,
)

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


class LonelyFactory:
    """A factory that makes no other element."""

    def __init__(self, factory: Any) -> None:
        self.factory = factory

    def get_name(self) -> str:
        return str(self.factory.get_name())

    def create(self, name: str | None) -> None:
        return None


class Lonely:
    def __init__(self, element: Any) -> None:
        self.element = element

    def get_factory(self) -> LonelyFactory:
        return LonelyFactory(self.element.get_factory())

    def list_properties(self) -> Any:
        return self.element.list_properties()

    def get_property(self, name: str) -> Any:
        return self.element.get_property(name)


def test_describe_without_a_fresh_element() -> None:
    """Compared with the properties' defaults instead."""
    sink: Any = Gst.ElementFactory.make("fakesink")
    sink.props.max_lateness = 5
    description = describe(Lonely(sink))
    assert description.startswith("fakesink ")
    assert "max-lateness=5" in description.split()
    make_sink(description)


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


class TargetSink:
    """pipewiresink's element for a device, as far as `name_target` sees it."""

    def __init__(self) -> None:
        self.properties: dict[str, str] = {"target-object": "4812"}

    def find_property(self, name: str) -> object | None:
        return name if name in self.properties else None

    def set_property(self, name: str, value: str) -> None:
        self.properties[name] = value


def test_name_target() -> None:
    sink = TargetSink()
    node = "alsa_output.usb-Yamaha_NX-N500-00.analog-stereo"
    name_target(sink, Gst.Structure.new_from_string(f"properties, node.name={node}"))
    assert sink.properties["target-object"] == node
    # Without a node name, the serial stays.
    sink = TargetSink()
    name_target(sink, Gst.Structure.new_from_string("properties, is-default=true"))
    assert sink.properties["target-object"] == "4812"
    # A sink without the property is left as it is.
    fakesink: Any = Gst.ElementFactory.make("fakesink")
    name_target(fakesink, Gst.Structure.new_from_string("properties, node.name=x"))
    assert describe(fakesink) == "fakesink"


def test_list_outputs() -> None:
    # Without a sound server (CI), there may be none.
    outputs = list_outputs()
    assert all(isinstance(o, Output) for o in outputs)
    assert outputs == sorted(outputs, key=lambda o: not o.default)
