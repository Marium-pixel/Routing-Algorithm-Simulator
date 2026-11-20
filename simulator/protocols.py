"""Link-State and Distance-Vector routing agents.

Every router is an independent agent.  Agents never read each other's state;
they only exchange messages through ``Simulation.send`` over links that have a
configurable one-way delay.

Link-State (OSPF-like)
    * each router originates a sequence-numbered LSA listing its up links
    * LSAs are flooded reliably (a router re-floods any LSA that is newer than
      what it has, to every neighbour except the one it came from)
    * when a link comes up the two ends exchange their whole LSDB (like OSPF
      database exchange) so that healed partitions resynchronise
    * every router runs Dijkstra over its own LSDB; an edge is used only when
      both ends advertise it (OSPF "two-way connectivity check")

Distance-Vector (RIP-like Bellman-Ford)
    * full table on start-up / when a link comes up, then *triggered* updates
      carrying only the destinations whose route changed
    * receivers merge the received entries into the stored vector of that
      neighbour, so an entry that is *not* sent stays stale (this is exactly why
      plain split horizon is weaker than poisoned reverse)
    * ``plain``          advertise the real metric to everybody
    * ``split-horizon``  do not advertise a destination to the neighbour that is
                         the next hop for it
    * ``poison-reverse`` advertise such destinations to that neighbour with
                         metric = infinity
    * metrics are capped at ``infinity`` (RIP uses 16)
"""

from __future__ import annotations

import heapq
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import Simulation


Route = tuple[int, "str | None"]  # (cost, next_hop)


class _Agent:
    def __init__(self, node: str, simulation: "Simulation") -> None:
        self.node = node
        self.sim = simulation
        self.routes: dict[str, Route] = {}

    def _install_routes(self, new_routes: dict[str, Route]) -> list[str]:
        """Merge (possibly partial) new routes into the table; return destinations that changed."""
        unreachable: Route = (self.sim.infinity, None)
        changed = []
        for destination, route in new_routes.items():
            old = self.routes.get(destination, unreachable)
            if route != old:
                changed.append(destination)
                self.sim.note_route_change(self.node, destination, old, route)
            self.routes[destination] = route
        return changed


class LinkStateAgent(_Agent):
    """OSPF-like flooding of locally originated link-state advertisements."""

    def __init__(self, node: str, simulation: "Simulation") -> None:
        super().__init__(node, simulation)
        self.sequence = 0
        self.lsdb: dict[str, tuple[int, dict[str, int]]] = {}

    # ---------------------------------------------------------------- hooks
    def start(self) -> None:
        self.originate_lsa()

    def link_changed(self, neighbor: str, event: str) -> None:
        self.originate_lsa()
        if event == "link_up":
            snapshot = [{"origin": o, "seq": s, "links": l} for o, (s, l) in self.lsdb.items()]
            self.sim.send(self.node, neighbor, "lsdb", {"lsas": snapshot})

    def receive(self, sender: str, kind: str, payload: dict) -> None:
        if kind == "lsa":
            items = [payload]
        elif kind == "lsdb":
            items = payload["lsas"]
        else:
            return
        fresh = [item for item in items if self._install(item)]
        if not fresh:
            return
        self.recompute()
        for item in fresh:
            for neighbor, _ in self.sim.topology.neighbors(self.node):
                if neighbor != sender:
                    self.sim.send(self.node, neighbor, "lsa", item)

    # ------------------------------------------------------------- internals
    def originate_lsa(self) -> None:
        self.sequence += 1
        adjacency = {n: link.cost for n, link in self.sim.topology.neighbors(self.node)}
        self.lsdb[self.node] = (self.sequence, adjacency)
        self.recompute()
        item = {"origin": self.node, "seq": self.sequence, "links": adjacency}
        for neighbor in adjacency:
            self.sim.send(self.node, neighbor, "lsa", item)

    def _install(self, item: dict) -> bool:
        origin, seq = item["origin"], int(item["seq"])
        old = self.lsdb.get(origin)
        if old is not None and seq <= old[0]:
            return False
        self.lsdb[origin] = (seq, {k: int(v) for k, v in item["links"].items()})
        return True

    def recompute(self) -> None:
        """Dijkstra over the local LSDB (two-way checked edges only)."""
        infinity = self.sim.infinity
        graph: dict[str, dict[str, int]] = {}
        for origin, (_, advertised) in self.lsdb.items():
            for neighbor, cost in advertised.items():
                back = self.lsdb.get(neighbor)
                if back is not None and origin in back[1]:
                    graph.setdefault(origin, {})[neighbor] = cost
        distances: dict[str, int] = {self.node: 0}
        first_hop: dict[str, str | None] = {self.node: None}
        queue: list[tuple[int, str]] = [(0, self.node)]
        while queue:
            cost, current = heapq.heappop(queue)
            if cost != distances[current]:
                continue
            for neighbor, edge_cost in graph.get(current, {}).items():
                candidate = cost + edge_cost
                if candidate >= infinity:
                    continue
                if candidate < distances.get(neighbor, infinity):
                    distances[neighbor] = candidate
                    first_hop[neighbor] = neighbor if current == self.node else first_hop[current]
                    heapq.heappush(queue, (candidate, neighbor))
        self._install_routes({
            node: (distances[node], first_hop[node]) if node in distances else (infinity, None)
            for node in self.sim.topology.nodes
        })


class DistanceVectorAgent(_Agent):
    """RIP-like triggered-update Bellman-Ford with selectable loop prevention."""

    def __init__(self, node: str, simulation: "Simulation", mode: str = "plain") -> None:
        super().__init__(node, simulation)
        if mode not in {"plain", "split-horizon", "poison-reverse"}:
            raise ValueError(f"Unknown DV mode: {mode}")
        self.mode = mode
        self.neighbor_vectors: dict[str, dict[str, int]] = {}

    # ---------------------------------------------------------------- hooks
    def start(self) -> None:
        # First recompute installs the route to ourselves (cost 0), which counts
        # as a change and is therefore advertised to every neighbour.
        self.recompute()

    def link_changed(self, neighbor: str, event: str) -> None:
        if self._direct_cost(neighbor) is None:
            self.neighbor_vectors.pop(neighbor, None)
        self.recompute()
        if event == "link_up":
            # New adjacency: send it our whole table (RIP "request/response").
            self.advertise(self._finite_destinations(), only=neighbor)

    def receive(self, sender: str, kind: str, payload: dict) -> None:
        if kind != "dv" or self._direct_cost(sender) is None:
            return
        vector = self.neighbor_vectors.setdefault(sender, {})
        for destination, metric in payload["entries"].items():
            vector[destination] = min(int(metric), self.sim.infinity)
        self.recompute(payload["entries"])  # only these destinations can change

    # ------------------------------------------------------------- internals
    def _direct_cost(self, neighbor: str) -> int | None:
        for candidate, link in self.sim.topology.neighbors(self.node):
            if candidate == neighbor:
                return link.cost
        return None

    def _finite_destinations(self) -> list[str]:
        return [d for d, (cost, _) in self.routes.items() if cost < self.sim.infinity]

    def _entries(self, neighbor: str, destinations: list[str]) -> dict[str, int]:
        entries: dict[str, int] = {}
        for destination in destinations:
            if destination == neighbor:
                continue
            cost, next_hop = self.routes.get(destination, (self.sim.infinity, None))
            if next_hop is not None and next_hop == neighbor:
                if self.mode == "split-horizon":
                    continue
                if self.mode == "poison-reverse":
                    cost = self.sim.infinity
            entries[destination] = cost
        return entries

    def advertise(self, destinations: list[str], only: str | None = None) -> None:
        if not destinations:
            return
        for neighbor, _ in self.sim.topology.neighbors(self.node):
            if only is not None and neighbor != only:
                continue
            entries = self._entries(neighbor, destinations)
            if entries:
                self.sim.send(self.node, neighbor, "dv", {"entries": entries})

    def recompute(self, destinations=None) -> None:
        infinity = self.sim.infinity
        links = self.sim.topology.neighbors(self.node)
        new_routes: dict[str, Route] = {}
        for destination in (self.sim.topology.nodes if destinations is None else destinations):
            if destination == self.node:
                new_routes[destination] = (0, None)
                continue
            best_cost, best_hop = infinity, None
            for neighbor, link in links:
                neighbor_cost = self.neighbor_vectors.get(neighbor, {}).get(destination, infinity)
                candidate = min(infinity, link.cost + neighbor_cost)
                if candidate < best_cost:
                    best_cost, best_hop = candidate, neighbor
            new_routes[destination] = (best_cost, best_hop)
        changed = self._install_routes(new_routes)
        if changed:
            self.advertise(changed)
