"""Command-line entry point for routing algorithm simulations.

Examples
--------
    python main.py examples/six_router.json --algorithm all
    python main.py examples/six_router.json --events examples/link_failure.json --output results
    python main.py examples/count_to_infinity.json --events examples/count_to_infinity_failure.json
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from simulator import ALGORITHMS, load_events, run_simulation, validate_routes
from simulator.topology import Topology


def print_routes(sim) -> None:
    for node, table in sim.route_table().items():
        print(f"\nRouting table for {node}")
        print(f"{'destination':<12} {'cost':<5} next hop")
        for destination, route in sorted(table.items()):
            cost = "inf" if route["cost"] >= sim.infinity else str(route["cost"])
            print(f"{destination:<12} {cost:<5} {route['next_hop'] or '-'}")


def print_metrics(sim) -> None:
    print(f"{'phase':<9} {'conv_ms':>8} {'quiesce_ms':>10} {'rounds':>7} {'messages':>9} {'bytes':>9} {'rt_changes':>10}")
    for phase in sim.phases:
        d = phase.as_dict()
        print(f"{d['phase']:<9} {d['convergence_ms']:>8} {d['quiescence_ms']:>10} {d['rounds']:>7} "
              f"{d['messages']:>9} {d['bytes']:>9} {d['route_changes']:>10}")


def write_outputs(sim, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    base = output_dir / sim.algorithm
    base.with_suffix(".events.json").write_text(json.dumps(sim.event_log, indent=2), encoding="utf-8")
    base.with_suffix(".routes.json").write_text(json.dumps(sim.route_table(), indent=2), encoding="utf-8")
    with base.with_suffix(".metrics.csv").open("w", newline="", encoding="utf-8") as file:
        fields = ["algorithm", "phase", "start_ms", "convergence_ms", "quiescence_ms", "rounds", "messages", "bytes", "route_changes"]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for phase in sim.phases:
            writer.writerow({"algorithm": sim.algorithm, **phase.as_dict()})


def main() -> None:
    parser = argparse.ArgumentParser(description="Link-State and Distance-Vector routing simulator")
    parser.add_argument("topology", help="Path to topology JSON")
    parser.add_argument("--algorithm", choices=[*ALGORITHMS, "all"], default="all")
    parser.add_argument("--events", help="Path to scheduled-events JSON (times are relative to initial convergence)")
    parser.add_argument("--infinity", type=int, default=16, help="Unreachable metric (default: 16, as in RIP)")
    parser.add_argument("--unit-costs", action="store_true", help="Set every link cost to 1 (hop count, RIP-style)")
    parser.add_argument("--output", help="Directory for metrics, routes and event logs")
    parser.add_argument("--quiet", action="store_true", help="Do not print routing tables")
    args = parser.parse_args()

    topology = Topology.from_json(args.topology)
    if args.unit_costs:
        topology = topology.unit_cost()
    events = load_events(args.events)
    for algorithm in (ALGORITHMS if args.algorithm == "all" else [args.algorithm]):
        sim = run_simulation(topology, algorithm, events, args.infinity)
        check = validate_routes(sim)
        status = "converged" if sim.converged else "DID NOT SETTLE (event limit)"
        print(f"\n=== {algorithm}: {status}; path-cost agreement with Dijkstra "
              f"{check['cost_ok']}/{check['pairs']}, loops={check['loops']} ===")
        print_metrics(sim)
        if not args.quiet:
            print_routes(sim)
        if args.output:
            write_outputs(sim, Path(args.output))


if __name__ == "__main__":
    main()
