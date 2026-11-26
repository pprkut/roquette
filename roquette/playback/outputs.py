# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The audio outputs GStreamer knows of, as sink descriptions.

An output is chosen by its description, which `Engine` and `--audio-sink`
(and the settings, M4) take: the sink element and the properties that
pick the device, e.g. "pulsesink device=alsa_output.…". Listing them
asks GStreamer's device providers, which connect to the sound server:
blocking, for a worker thread or the command line.
"""

from dataclasses import dataclass
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GObject, Gst  # noqa: E402

__all__ = ["Output", "describe", "list_outputs"]

# Properties that don't pick a device, and can't be given in a description.
_SKIP = {"name", "parent"}
_TYPES = (
    GObject.TYPE_BOOLEAN,
    GObject.TYPE_INT,
    GObject.TYPE_UINT,
    GObject.TYPE_INT64,
    GObject.TYPE_UINT64,
    GObject.TYPE_DOUBLE,
    GObject.TYPE_FLOAT,
    GObject.TYPE_STRING,
)


@dataclass(frozen=True, slots=True)
class Output:
    # The device's name, for people.
    name: str
    # The sink's description.
    sink: str
    # The sound server's default output.
    default: bool = False


def list_outputs() -> list[Output]:
    """The audio outputs of GStreamer's device providers (PipeWire,
    PulseAudio, ALSA, as installed), the default one first."""
    Gst.init([])
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Audio/Sink", None)
    if not monitor.start():
        return []
    try:
        devices = monitor.get_devices() or []
    finally:
        monitor.stop()
    outputs = [output for d in devices if (output := output_of(d)) is not None]
    return sorted(outputs, key=lambda o: not o.default)


def output_of(device: Any) -> Output | None:
    """The output of a `Gst.Device`, if it makes an element."""
    element = device.create_element(None)
    if element is None:
        return None
    default = False
    properties = device.get_properties()
    if properties is not None:
        ok, value = properties.get_boolean("is-default")
        default = bool(ok and value)
    return Output(str(device.get_display_name()), describe(element), default)


def describe(element: Any) -> str:
    """A description that makes `element` again: its factory and the
    properties set to something else than a new one of its kind has (or,
    if the factory makes no other, than the properties' defaults)."""
    factory = element.get_factory()
    fresh = factory.create(None)
    parts = [str(factory.get_name())]
    for spec in element.list_properties():
        flags = spec.flags
        if (
            spec.name in _SKIP
            or not flags & GObject.ParamFlags.READABLE
            or not flags & GObject.ParamFlags.WRITABLE
        ):
            continue
        value = element.get_property(spec.name)
        if fresh is not None:
            unset = fresh.get_property(spec.name)
        else:
            unset = spec.get_default_value()
        if value == unset:
            continue
        text = _serialize(spec, value)
        if text is not None:
            parts.append(f"{spec.name}={text}")
    return " ".join(parts)


def _serialize(spec: Any, value: Any) -> str | None:
    if spec.value_type == GObject.TYPE_STRING:
        if value is None:
            return None
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if spec.value_type in _TYPES or spec.value_type.is_a(GObject.TYPE_ENUM):
        text = Gst.value_serialize(GObject.Value(spec.value_type, value))
        return str(text) if text is not None else None
    # Objects, boxed values, flags: not what picks a device.
    return None
