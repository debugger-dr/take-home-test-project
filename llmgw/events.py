from dataclasses import dataclass, field
from typing import Protocol


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


class MemorySink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, name: str, now: float, **fields: object) -> None:
        self.events.append(Event(name, now, fields))

    def named(self, name: str) -> list[Event]:
        return [e for e in self.events if e.name == name]
