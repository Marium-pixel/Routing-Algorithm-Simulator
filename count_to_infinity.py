"""Count-to-infinity study: plain DV vs split horizon vs poisoned reverse (LS for reference).

Scenarios
---------
line         X-Y-Z, link Y-Z fails.  Plain DV: X and Y feed each other ever larger
             costs for Z until the unreachable metric (16, RIP-compatible).
triangle     Textbook cost-increase example: X-Y 4 -> 60 with X-Z 50, Y-Z 1.
loop_of_three  Triangle A-B-C with a stub D on A; D-A fails.  Poisoned reverse
             stops 2-node loops only, so the three-node loop still counts up.

Outputs (default ``results/``): count_to_infinity.csv (summary) and
count_to_infinity_trace.csv (cost of the watched route over time, used by plots.py).
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from simulator import ALGORITHMS, load_events, run_simulation, validate_routes
from simulator.topology import Topology

EX = Path(__file__).parent / "examples"
SCENARIOS = {
    # name: (topology, events, infinity, destination being watched, nodes whose cost to it is traced)
    "line": ("count_to_infinity.json", "count_to_infinity_failure.json", 16, "Z", ["X", "Y"]),
    "triangle": ("triangle_cost_increase.json", "triangle_cost_increase_event.json", 100, "X", ["Y", "Z"]),
    "loop_of_three": ("loop_of_three.json", "loop_of_three_failure.json", 16, "D", ["A", "B", "C"]),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", default="results")
    parser.add_argument("--scenarios", nargs="+", default=list(SCENARIOS), choices=list(SCENARIOS))
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    summary, trace = [], []
    print(f"{'scenario':<14} {'algorithm':<18} {'rounds':>6} {'msgs':>5} {'bytes':>6} {'time_ms':>7} {'peak':>5} {'loops':>5} {'cost_ok':>8}")
    for name in args.scenarios:
        topo_file, event_file, infinity, dest, watch = SCENARIOS[name]
        topology = Topology.from_json(EX / topo_file)
        events = load_events(EX / event_file)
        for algorithm in ALGORITHMS:
            sim = run_simulation(topology, algorithm, events, infinity, record_routes=True)
            phase = sim.get_phase("events")
            check = validate_routes(sim)
            t0 = phase.start_ms
            peak = 0
            for node in watch:
                for time_ms, cost in sim.cost_trace(node, dest):
                    if time_ms >= t0:
                        trace.append({"scenario": name, "algorithm": algorithm, "node": node,
                                      "time_ms": time_ms - t0, "cost": cost})
                        if cost < infinity:
                            peak = max(peak, cost)
            row = {"scenario": name, "algorithm": algorithm, "infinity": infinity, "destination": dest,
                   "rounds": phase.rounds, "messages": phase.messages, "bytes": phase.bytes,
                   "duration_ms": phase.convergence_ms, "peak_finite_cost": peak,
                   "loops": check["loops"], "cost_ok": check["cost_ok"], "pairs": check["pairs"]}
            summary.append(row)
            print(f"{name:<14} {algorithm:<18} {row['rounds']:>6} {row['messages']:>5} {row['bytes']:>6} "
                  f"{row['duration_ms']:>7} {peak:>5} {row['loops']:>5} {row['cost_ok']:>4}/{row['pairs']:<3}")

    with (out / "count_to_infinity.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    with (out / "count_to_infinity_trace.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["scenario", "algorithm", "node", "time_ms", "cost"])
        writer.writeheader()
        writer.writerows(trace)
    print(f"\nWrote {out / 'count_to_infinity.csv'} and {out / 'count_to_infinity_trace.csv'}")


if __name__ == "__main__":
    main()
