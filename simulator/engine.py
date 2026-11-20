"""A minimal deterministic discrete-event scheduler."""

from __future__ import annotations

from dataclasses import dataclass, field
import heapq
from itertools import count
from typing import Callable, Any


@dataclass(order=True)
class ScheduledEvent:
    time_ms: int
    order: int
    callback: Callable[..., Any] = field(compare=False)
    args: tuple[Any, ...] = field(compare=False, default=())


class EventEngine:
    def __init__(self) -> None:
        self.now_ms = 0
        self._sequence = count()
        self._queue: list[ScheduledEvent] = []

    def schedule_at(self, time_ms: int, callback: Callable[..., Any], *args: Any) -> None:
        if time_ms < self.now_ms:
            raise ValueError("Cannot schedule an event in the past")
        heapq.heappush(self._queue, ScheduledEvent(time_ms, next(self._sequence), callback, args))

    def schedule_in(self, delay_ms: int, callback: Callable[..., Any], *args: Any) -> None:
        self.schedule_at(self.now_ms + delay_ms, callback, *args)

    def run(self, until_ms: int | None = None, max_events: int = 1_000_000) -> int:
        handled = 0
        while self._queue and (until_ms is None or self._queue[0].time_ms <= until_ms):
            if handled >= max_events:
                raise RuntimeError("Event limit reached; protocol did not settle")
            event = heapq.heappop(self._queue)
            self.now_ms = event.time_ms
            event.callback(*event.args)
            handled += 1
        if until_ms is not None:
            self.now_ms = max(self.now_ms, until_ms)
        return handled

    @property
    def pending(self) -> int:
        return len(self._queue)

