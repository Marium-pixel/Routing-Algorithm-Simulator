"""Topology representation, JSON loading and random topology generation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import random

import networkx as nx


@dataclass
class Link:
    a: str
    b: str
    cost: int
    delay_ms: int
    up: bool = True

    def other(self, node: str) -> str:
        if node == self.a:
            return self.b
        if node == self.b:
            return self.a
        raise ValueError(f"{node} is not on {self.a}-{self.b}")


class Topology:
    def __init__(self, nodes: list[str], links: list[Link]):
        if len(nodes) != len(set(nodes)):
            raise ValueError("Node names must be unique")
        self.nodes = list(nodes)
        self.links = links
        self._links: dict[frozenset[str], Link] = {}
        for link in links:
            if link.a not in self.nodes or link.b not in self.nodes:
                raise ValueError(f"Link endpoint missing from nodes: {link}")
            key = frozenset((link.a, link.b))
            if len(key) != 2 or key in self._links:
                raise ValueError(f"Duplicate or invalid link: {link.a}-{link.b}")
            if link.cost < 1 or link.delay_ms < 0:
                raise ValueError("Link cost must be >= 1 and delay_ms must be >= 0")
            self._links[key] = link

    # ------------------------------------------------------------------ I/O
    @classmethod
    def from_json(cls, path: str | Path) -> "Topology":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        links = [
            Link(
                a=item["a"], b=item["b"], cost=int(item.get("cost", 1)),
                delay_ms=int(item.get("delay_ms", 1)), up=bool(item.get("up", True)),
            )
            for item in data["links"]
        ]
        return cls(data["nodes"], links)

    def to_dict(self) -> dict:
        return {
            "nodes": list(self.nodes),
            "links": [
                {"a": l.a, "b": l.b, "cost": l.cost, "delay_ms": l.delay_ms, "up": l.up}
                for l in self.links
            ],
        }

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    # -------------------------------------------------------------- queries
    def link(self, a: str, b: str) -> Link:
        try:
            return self._links[frozenset((a, b))]
        except KeyError as exc:
            raise ValueError(f"No link exists between {a} and {b}") from exc

    def neighbors(self, node: str, only_up: bool = True) -> list[tuple[str, Link]]:
        result = []
        for link in self.links:
            if node in (link.a, link.b) and (link.up or not only_up):
                result.append((link.other(node), link))
        return result

    def clone(self) -> "Topology":
        return Topology(self.nodes, [Link(**vars(link)) for link in self.links])

    def to_networkx(self, only_up: bool = True) -> nx.Graph:
        graph = nx.Graph()
        graph.add_nodes_from(self.nodes)
        for link in self.links:
            if link.up or not only_up:
                graph.add_edge(link.a, link.b, weight=link.cost, delay=link.delay_ms)
        return graph

    def unit_cost(self) -> "Topology":
        """Copy with every link cost set to 1 (RIP hop-count comparison)."""
        copy = self.clone()
        for link in copy.links:
            link.cost = 1
        return copy

    def with_random_delays(self, seed: int, low: int = 1, high: int = 10) -> "Topology":
        """Copy with per-link delays drawn from a seeded RNG."""
        rng = random.Random(seed)
        copy = self.clone()
        for link in copy.links:
            link.delay_ms = rng.randint(low, high)
        return copy

    def weighted_diameter(self) -> int:
        graph = self.to_networkx()
        best = 0
        for _, dist in nx.all_pairs_dijkstra_path_length(graph, weight="weight"):
            best = max(best, max(dist.values()))
        return best

    def non_bridge_links(self) -> list[Link]:
        """Links whose removal keeps the graph connected."""
        graph = self.to_networkx()
        bridges = {frozenset(edge) for edge in nx.bridges(graph)}
        return [l for l in self.links if l.up and frozenset((l.a, l.b)) not in bridges]


def random_topology(
    size: int, seed: int, cost_range: tuple[int, int] = (1, 10),
    delay_range: tuple[int, int] = (1, 10), extra_edge_ratio: float = 0.5,
) -> Topology:
    """Seeded random *connected* graph: a random spanning tree plus extra edges.

    Average degree is about 2 * (1 + extra_edge_ratio), i.e. 3 by default.
    """
    if size < 2:
        raise ValueError("size must be >= 2")
    rng = random.Random(seed)
    nodes = [f"R{i}" for i in range(1, size + 1)]
    order = nodes[:]
    rng.shuffle(order)
    edges: set[frozenset[str]] = set()
    for i in range(1, size):
        edges.add(frozenset((order[i], order[rng.randrange(i)])))
    target = min(size - 1 + int(size * extra_edge_ratio), size * (size - 1) // 2)
    while len(edges) < target:
        a, b = rng.sample(nodes, 2)
        edges.add(frozenset((a, b)))
    links = []
    for edge in sorted(edges, key=lambda e: sorted(e)):
        a, b = sorted(edge)
        links.append(Link(a, b, cost=rng.randint(*cost_range), delay_ms=rng.randint(*delay_range)))
    return Topology(nodes, links)
