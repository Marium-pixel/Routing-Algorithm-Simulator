"""Check simulated routing tables against ground-truth shortest paths (NetworkX)."""

from __future__ import annotations

from typing import Any

import networkx as nx

from .runner import Simulation


def expected_costs(sim: Simulation) -> dict[str, dict[str, int]]:
    """All-pairs shortest-path cost over the links that are currently up."""
    graph = sim.topology.to_networkx(only_up=True)
    return {src: dict(dist) for src, dist in nx.all_pairs_dijkstra_path_length(graph, weight="weight")}


def validate_routes(sim: Simulation) -> dict[str, Any]:
    """Compare every (source, destination) pair with Dijkstra from NetworkX.

    * cost_ok       the simulator cost equals the shortest-path cost (or both are
                    unreachable, i.e. >= the protocol's infinity)
    * next_hop_ok   following the advertised next hop is consistent with a shortest
                    path (link cost + remaining distance == total cost); ties are
                    therefore accepted whichever equal-cost hop was chosen
    * loops         routes whose next-hop chain revisits a router
    """
    infinity = sim.infinity
    truth = expected_costs(sim)
    routes = sim.route_table()
    pairs = cost_ok = hop_ok = loops = 0
    mismatches: list[dict[str, Any]] = []
    for src in sim.topology.nodes:
        for dst in sim.topology.nodes:
            if src == dst:
                continue
            pairs += 1
            want = truth[src].get(dst)
            want = want if want is not None and want < infinity else infinity
            got = routes[src][dst]["cost"]
            if got == want:
                cost_ok += 1
            else:
                mismatches.append({"src": src, "dst": dst, "expected": want, "got": got})
            hop = routes[src][dst]["next_hop"]
            if want >= infinity:
                hop_ok += int(hop is None)
            elif hop is not None and sim.topology.link(src, hop).up:
                rest = truth[hop].get(dst, infinity) if hop != dst else 0
                hop_ok += int(sim.topology.link(src, hop).cost + rest == want)
            # loop check
            seen, current = {src}, src
            while current != dst:
                nxt = routes[current][dst]["next_hop"]
                if nxt is None:
                    break
                if nxt in seen:
                    loops += 1
                    break
                seen.add(nxt)
                current = nxt
    return {
        "pairs": pairs, "cost_ok": cost_ok, "next_hop_ok": hop_ok, "loops": loops,
        "cost_agreement": cost_ok / pairs if pairs else 1.0, "mismatches": mismatches[:20],
    }
