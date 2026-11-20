"""Compare FRR routing tables (saved by `frrctl.py tables`) with the simulator.

For every ordered router pair (Ri -> Rj) the loopback 10.255.0.j/32 is looked up in Ri's
FRR table and compared with the simulator's route cost to Rj.

    # OSPF (explicit costs from examples/six_router.json), stable network
    python frr/compare_with_simulator.py --protocol ospf --frr-dir results/frr/ospf/stable
    # after failing R1-R2 in the lab (and in the simulator)
    python frr/compare_with_simulator.py --protocol ospf --frr-dir results/frr/ospf/fail_r1_r2 --down R1-R2
    # RIP uses hop count; FRR's RIP metric of a remote connected prefix is hops + 1
    python frr/compare_with_simulator.py --protocol rip --frr-dir results/frr/rip/stable

Next hops: FRR next-hop IPs are mapped to routers; a route is "next-hop OK" when every FRR
next hop lies on *some* shortest path (so ECMP/ties are accepted).  The simulator's own next
hop is checked the same way.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import networkx as nx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lab_model import DEFAULT_TOPOLOGY, build  # noqa: E402
from simulator import run_simulation  # noqa: E402
from simulator.topology import Topology  # noqa: E402

RIP_METRIC_OFFSET = 1  # a neighbour's connected prefix is received with metric 1 + link cost (1)


def parse_frr_route(table: dict, prefix: str, protocol: str):
    """Return (metric, [next-hop IPs]) of the selected route for ``prefix`` or None."""
    for entry in table.get(prefix, []):
        if entry.get("protocol") == protocol and entry.get("selected", True):
            hops = [nh["ip"] for nh in entry.get("nexthops", []) if "ip" in nh]
            return int(entry["metric"]), hops
    return None


def valid_next_hops(graph: nx.Graph, src: str, dst: str) -> set[str]:
    dist = nx.single_source_dijkstra_path_length(graph, dst, weight="weight")
    if src not in dist:
        return set()
    return {n for n in graph.neighbors(src) if graph[src][n]["weight"] + dist[n] == dist[src]}


def compare(protocol: str, frr_dir: Path, topology_path: Path, down: list[str], algorithm: str) -> list[dict]:
    lab = build(topology_path)
    owner = lab.ip_owner()
    topology = Topology.from_json(topology_path)
    if protocol == "rip":
        topology = topology.unit_cost()
    events = []
    for pair in down:
        a, b = pair.split("-")
        events.append({"time_ms": 0, "type": "link_down", "a": a, "b": b})
    infinity = 16 if protocol == "rip" else 10_000
    sim = run_simulation(topology, algorithm, events, infinity)
    routes = sim.route_table()
    graph = sim.topology.to_networkx(only_up=True)
    offset = RIP_METRIC_OFFSET if protocol == "rip" else 0

    rows = []
    for src in lab.routers:
        table = json.loads((frr_dir / f"{src}.json").read_text(encoding="utf-8"))
        for dst in lab.routers:
            if src == dst:
                continue
            frr = parse_frr_route(table, f"{lab.loopbacks[dst]}/32", protocol)
            sim_cost = routes[src][dst]["cost"]
            reachable = sim_cost < infinity
            frr_metric, frr_hops = (frr if frr else (None, []))
            frr_hop_routers = sorted({owner.get(ip, ip) for ip in frr_hops})
            valid = valid_next_hops(graph, src, dst)
            sim_hop = routes[src][dst]["next_hop"]
            rows.append({
                "src": src, "dst": dst,
                "sim_cost": sim_cost if reachable else "unreachable",
                "frr_metric": frr_metric if frr_metric is not None else "none",
                "frr_expected": sim_cost + offset if reachable else "none",
                "cost_match": int((frr_metric == sim_cost + offset) if reachable else frr is None),
                "sim_next_hop": sim_hop or "-",
                "frr_next_hops": ",".join(frr_hop_routers) or "-",
                "next_hop_match": int((bool(frr_hop_routers) and set(frr_hop_routers) <= valid and sim_hop in valid)
                                      if reachable else not frr_hop_routers),
            })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--protocol", choices=["ospf", "rip"], required=True)
    parser.add_argument("--frr-dir", required=True, type=Path)
    parser.add_argument("--topology", default=str(DEFAULT_TOPOLOGY), type=Path)
    parser.add_argument("--down", nargs="*", default=[], help="links that are down in the lab, e.g. R1-R2")
    parser.add_argument("--algorithm", default="ls", choices=["ls", "dv", "dv-split-horizon", "dv-poison-reverse"])
    parser.add_argument("--csv", type=Path, help="write the comparison table here")
    parser.add_argument("--strict", action="store_true", help="exit with status 1 on any mismatch")
    args = parser.parse_args()

    rows = compare(args.protocol, args.frr_dir, args.topology, args.down, args.algorithm)
    print(f"{'src':<4} {'dst':<4} {'sim':>11} {'frr':>5} {'frr_exp':>8} {'cost':>5} {'sim_nh':<7} {'frr_nh':<10} {'nh':>3}")
    for r in rows:
        print(f"{r['src']:<4} {r['dst']:<4} {r['sim_cost']:>11} {r['frr_metric']:>5} {r['frr_expected']:>8} "
              f"{'OK' if r['cost_match'] else 'DIFF':>5} {r['sim_next_hop']:<7} {r['frr_next_hops']:<10} "
              f"{'OK' if r['next_hop_match'] else 'DIFF':>3}")
    cost_ok = sum(r["cost_match"] for r in rows)
    hop_ok = sum(r["next_hop_match"] for r in rows)
    print(f"\npath-cost agreement: {cost_ok}/{len(rows)} ({100 * cost_ok / len(rows):.1f}%)   "
          f"next-hop agreement: {hop_ok}/{len(rows)}")
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    if args.strict and (cost_ok != len(rows) or hop_ok != len(rows)):
        sys.exit(1)


if __name__ == "__main__":
    main()
