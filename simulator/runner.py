"""Simulation orchestration, message accounting, and event handling."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from .engine import EventEngine
from .protocols import DistanceVectorAgent, LinkStateAgent
from .topology import Topology

ALGORITHMS = ["ls", "dv", "dv-split-horizon", "dv-poison-reverse"]
_DV_MODES = {"dv": "plain", "dv-split-horizon": "split-horizon", "dv-poison-reverse": "poison-reverse"}


@dataclass
class PhaseMetrics:
    """Metrics for one measurement window (e.g. initial start-up, or after a failure).

    convergence_ms  simulated time from the start of the phase to the last routing
                    table change anywhere in the network
    quiescence_ms   simulated time until the last message was delivered
                    (>= convergence_ms; LS keeps flooding duplicates after tables settle)
    rounds          longest causal chain of messages, i.e. update rounds: a message
                    sent because of an external event is round 1, a message sent
                    because of a round-k message is round k+1
    """

    name: str
    start_ms: int = 0
    messages: int = 0
    bytes: int = 0
    rounds: int = 0
    route_changes: int = 0
    last_change_ms: int | None = None
    last_activity_ms: int | None = None

    @property
    def convergence_ms(self) -> int:
        if self.last_change_ms is None:
            return 0
        return max(0, self.last_change_ms - self.start_ms)

    @property
    def quiescence_ms(self) -> int:
        if self.last_activity_ms is None:
            return 0
        return max(0, self.last_activity_ms - self.start_ms)

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.name, "start_ms": self.start_ms,
            "convergence_ms": self.convergence_ms, "quiescence_ms": self.quiescence_ms,
            "rounds": self.rounds, "messages": self.messages, "bytes": self.bytes,
            "route_changes": self.route_changes,
        }


@dataclass
class Metrics:
    """Whole-run totals (kept for convenience)."""

    messages: int = 0
    bytes: int = 0
    rounds: int = 0
    convergence_ms: int = 0


class Simulation:
    def __init__(self, topology: Topology, algorithm: str, infinity: int = 16, record_routes: bool = False) -> None:
        self.topology = topology
        self.algorithm = algorithm
        self.infinity = infinity
        self.record_routes = record_routes
        self.engine = EventEngine()
        self.metrics = Metrics()
        self.converged = True
        self.event_log: list[dict[str, Any]] = []
        self.route_log: list[dict[str, Any]] = []
        self.phases: list[PhaseMetrics] = []
        self.phase: PhaseMetrics | None = None
        self._generation = 0
        self.agents: dict[str, LinkStateAgent | DistanceVectorAgent] = {}
        for node in topology.nodes:
            if algorithm == "ls":
                self.agents[node] = LinkStateAgent(node, self)
            elif algorithm in _DV_MODES:
                self.agents[node] = DistanceVectorAgent(node, self, _DV_MODES[algorithm])
            else:
                raise ValueError(f"Unknown algorithm: {algorithm}")

    # ------------------------------------------------------------- phases
    def begin_phase(self, name: str, start_ms: int | None = None) -> PhaseMetrics:
        self.phase = PhaseMetrics(name, self.engine.now_ms if start_ms is None else start_ms)
        self.phases.append(self.phase)
        return self.phase

    def get_phase(self, name: str) -> PhaseMetrics | None:
        return next((p for p in self.phases if p.name == name), None)

    # ---------------------------------------------------- agent-facing API
    def send(self, sender: str, receiver: str, kind: str, payload: dict) -> None:
        try:
            link = self.topology.link(sender, receiver)
        except ValueError:
            return
        if not link.up:
            return
        # A defined JSON wire representation makes byte measurements repeatable.
        size = len(json.dumps({"type": kind, "payload": payload}, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        generation = self._generation + 1
        arrival = self.engine.now_ms + link.delay_ms
        self.metrics.messages += 1
        self.metrics.bytes += size
        self.metrics.rounds = max(self.metrics.rounds, generation)
        if self.phase is not None:
            self.phase.messages += 1
            self.phase.bytes += size
            self.phase.rounds = max(self.phase.rounds, generation)
            self.phase.last_activity_ms = max(self.phase.last_activity_ms or 0, arrival)
        self.event_log.append({"time_ms": self.engine.now_ms, "action": "send", "from": sender, "to": receiver,
                               "type": kind, "bytes": size, "round": generation})
        self.engine.schedule_at(arrival, self._deliver, sender, receiver, kind, payload, generation)

    def note_route_change(self, node: str, destination: str, old: tuple, new: tuple) -> None:
        now = self.engine.now_ms
        self.metrics.convergence_ms = now
        if self.phase is not None:
            self.phase.route_changes += 1
            self.phase.last_change_ms = now
        if self.record_routes:
            self.route_log.append({"time_ms": now, "node": node, "destination": destination,
                                   "old_cost": old[0], "cost": new[0], "next_hop": new[1]})

    # ------------------------------------------------------------ internals
    def _deliver(self, sender: str, receiver: str, kind: str, payload: dict, generation: int) -> None:
        link = self.topology.link(sender, receiver)
        if not link.up:
            return
        self.event_log.append({"time_ms": self.engine.now_ms, "action": "receive", "from": sender, "to": receiver, "type": kind})
        self._generation = generation
        self.agents[receiver].receive(sender, kind, payload)

    def start(self) -> None:
        self._generation = 0
        for agent in self.agents.values():
            agent.start()

    def apply_event(self, event: dict[str, Any]) -> None:
        self._generation = 0
        event_type, a, b = event["type"], event["a"], event["b"]
        link = self.topology.link(a, b)
        if event_type == "link_down":
            link.up = False
        elif event_type == "link_up":
            link.up = True
        elif event_type == "cost_change":
            cost = int(event["cost"])
            if cost < 1:
                raise ValueError("New link cost must be >= 1")
            link.cost = cost
        else:
            raise ValueError(f"Unknown event type: {event_type}")
        self.event_log.append({"time_ms": self.engine.now_ms, "action": event_type, "a": a, "b": b, "cost": link.cost})
        self.agents[a].link_changed(b, event_type)
        self._generation = 0
        self.agents[b].link_changed(a, event_type)

    def route_table(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {
            node: {d: {"cost": cost, "next_hop": hop} for d, (cost, hop) in agent.routes.items()}
            for node, agent in self.agents.items()
        }

    def cost_trace(self, node: str, destination: str) -> list[tuple[int, int]]:
        """(time_ms, cost) every time ``node``'s route to ``destination`` changed.

        Needs ``record_routes=True``.
        """
        return [(r["time_ms"], r["cost"]) for r in self.route_log if r["node"] == node and r["destination"] == destination]


def load_events(path: str | Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Events JSON must be a list")
    return sorted(data, key=lambda item: int(item["time_ms"]))


def run_simulation(
    topology: Topology, algorithm: str, events: list[dict[str, Any]] | None = None,
    infinity: int = 16, record_routes: bool = False, max_events: int = 5_000_000,
) -> Simulation:
    """Run start-up to convergence, then apply ``events`` and run to convergence again.

    Event ``time_ms`` values are relative to the moment the initial convergence
    finished, so the same events file works for any topology size.  Two phases are
    recorded: ``initial`` and (if there are events) ``events``.
    """
    sim = Simulation(topology.clone(), algorithm, infinity, record_routes)
    try:
        sim.begin_phase("initial", 0)
        sim.start()
        sim.engine.run(max_events=max_events)
        if events:
            base = sim.engine.now_ms
            events = sorted(events, key=lambda item: int(item["time_ms"]))
            sim.begin_phase("events", base + int(events[0]["time_ms"]))
            for event in events:
                sim.engine.schedule_at(base + int(event["time_ms"]), sim.apply_event, event)
            sim.engine.run(max_events=max_events)
    except RuntimeError:
        sim.converged = False
    sim.phase = None
    return sim
