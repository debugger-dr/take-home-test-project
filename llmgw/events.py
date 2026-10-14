import json
from dataclasses import dataclass, field
from typing import Protocol, TextIO


@dataclass(frozen=True, slots=True)
class Event:
    name: str
    at: float
    fields: dict[str, object] = field(default_factory=dict)


class EventSink(Protocol):
    def emit(self, name: str, now: float, **fields: object) -> None: ...


class NullSink:
    def emit(self, name: str, now: float, **fields: object) -> None:
        pass


class JsonLinesSink:
    """One JSON object per line, ready to ship to a log pipeline."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, name: str, now: float, **fields: object) -> None:
        record = {"event": name, "ts": round(now, 6), **fields}
        self._stream.write(json.dumps(record, default=str) + "\n")


class MemorySink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, name: str, now: float, **fields: object) -> None:
        self.events.append(Event(name, now, fields))

    def named(self, name: str) -> list[Event]:
        return [e for e in self.events if e.name == name]
