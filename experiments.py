"""Repeatable experiments: convergence, messages, bytes and scalability.

Conditions (each repeated with seeds 1, 2, 3; the simulator is deterministic, so the
seed changes the generated topology, link costs and link delays instead):

    stable        start-up convergence from empty tables
    cost_change   one link's cost is raised by COST_INCREASE after convergence
    link_failure  one (non-bridge) link goes down after convergence

Suites:

    random   random connected graphs of --sizes nodes (default 10 50 200)
    six      the six-router topology that mirrors the FRR lab (examples/six_router.json),
             both with its weighted costs (OSPF comparison) and unit costs (RIP comparison)

Outputs (``--output`` directory): raw.csv (one row per run), summary.csv and summary.md
(mean and sample standard deviation over seeds).

Example:  python experiments.py --suite all --jobs 2 --output results
"""

from __future__ import annotations

import argparse
import csv
import random
import statistics
import time
from multiprocessing import Pool
from pathlib import Path

from simulator import ALGORITHMS, run_simulation, validate_routes
from simulator.topology import Topology, random_topology

EX = Path(__file__).parent / "examples"
COST_INCREASE = 10
SEEDS = (1, 2, 3)
METRICS = ["convergence_ms", "quiescence_ms", "rounds", "messages", "bytes", "cost_agreement", "loops"]
RAW_FIELDS = ["suite", "topology", "nodes", "links", "seed", "algorithm", "condition", "infinity",
              "converged", *METRICS]


def pick_events(topology: Topology, condition: str, seed: int) -> list[dict]:
    rng = random.Random(seed * 7919 + len(topology.nodes))
    if condition == "link_failure":
        link = rng.choice(topology.non_bridge_links())
        return [{"time_ms": 0, "type": "link_down", "a": link.a, "b": link.b}]
    if condition == "cost_change":
        link = rng.choice(topology.links)
        return [{"time_ms": 0, "type": "cost_change", "a": link.a, "b": link.b, "cost": link.cost + COST_INCREASE}]
    raise ValueError(condition)


def make_tasks(args) -> list[dict]:
    tasks = []
    if args.suite in ("random", "all"):
        for size in args.sizes:
            for seed in SEEDS:
                topology = random_topology(size, seed)
                # Weighted metric: the unreachable value must exceed any real path cost.
                infinity = 2 * topology.weighted_diameter() + 2 * COST_INCREASE
                for algorithm in args.algorithms:
                    for condition in ("cost_change", "link_failure"):
                        tasks.append(dict(suite="random", name=f"random{size}", topology=topology, seed=seed,
                                          algorithm=algorithm, condition=condition, infinity=infinity,
                                          events=pick_events(topology, condition, seed)))
    if args.suite in ("six", "all"):
        base = Topology.from_json(EX / "six_router.json")
        variants = {"six_weighted": base, "six_unit": base.unit_cost()}
        six_events = {
            "link_failure": [{"time_ms": 0, "type": "link_down", "a": "R1", "b": "R2"}],
            "link_failure_r2r3": [{"time_ms": 0, "type": "link_down", "a": "R2", "b": "R3"}],
            "cost_change": [{"time_ms": 0, "type": "cost_change", "a": "R2", "b": "R3", "cost": 10}],
        }
        for name, topology in variants.items():
            for seed in SEEDS:
                with_delays = topology.with_random_delays(seed)
                for algorithm in args.algorithms:
                    for condition, events in six_events.items():
                        if name == "six_unit" and condition == "cost_change":
                            continue  # RIP is hop-count only: the cost-change test is OSPF-only
                        tasks.append(dict(suite="six", name=name, topology=with_delays, seed=seed,
                                          algorithm=algorithm, condition=condition, infinity=16, events=events))
    return tasks


def run_task(task: dict) -> list[dict]:
    topology: Topology = task["topology"]
    sim = run_simulation(topology, task["algorithm"], task["events"], task["infinity"])
    check = validate_routes(sim)
    rows = []
    base = {"suite": task["suite"], "topology": task["name"], "nodes": len(topology.nodes),
            "links": len(topology.links), "seed": task["seed"], "algorithm": task["algorithm"],
            "infinity": task["infinity"], "converged": int(sim.converged)}
    # The "stable" row is the initial phase (identical for every condition), emitted once.
    if task["condition"] in ("cost_change", "link_failure"):
        initial = sim.get_phase("initial").as_dict()
        rows.append({**base, "condition": "stable", **{k: initial[k] for k in METRICS if k in initial},
                     "cost_agreement": 1.0, "loops": 0})
    event_phase = sim.get_phase("events")
    if event_phase is not None:
        d = event_phase.as_dict()
        rows.append({**base, "condition": task["condition"], **{k: d[k] for k in METRICS if k in d},
                     "cost_agreement": round(check["cost_agreement"], 6), "loops": check["loops"]})
    return rows


def dedupe_stable(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for row in rows:
        if row["condition"] == "stable":
            key = (row["suite"], row["topology"], row["seed"], row["algorithm"])
            if key in seen:
                continue
            seen.add(key)
        out.append(row)
    return out


def summarize(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault((row["suite"], row["topology"], row["nodes"], row["algorithm"], row["condition"]), []).append(row)
    summary = []
    for (suite, topo, nodes, algorithm, condition), items in groups.items():
        out = {"suite": suite, "topology": topo, "nodes": nodes, "algorithm": algorithm,
               "condition": condition, "runs": len(items), "all_converged": int(all(r["converged"] for r in items))}
        for metric in METRICS:
            values = [float(r[metric]) for r in items]
            out[f"{metric}_mean"] = round(statistics.mean(values), 3)
            out[f"{metric}_std"] = round(statistics.stdev(values), 3) if len(values) > 1 else 0.0
        summary.append(out)
    order = {"stable": 0, "cost_change": 1, "link_failure": 2, "link_failure_r2r3": 3}
    summary.sort(key=lambda r: (r["suite"], r["nodes"], r["topology"], order.get(r["condition"], 9), ALGORITHMS.index(r["algorithm"])))
    return summary


def write_markdown(summary: list[dict], path: Path) -> None:
    lines = ["# Experiment summary (mean ± sample std over seeds 1-3)", ""]
    for key in sorted({(r["suite"], r["topology"], r["nodes"]) for r in summary}, key=lambda k: (k[0], k[2], k[1])):
        lines += [f"## {key[1]} ({key[2]} nodes)", "",
                  "| condition | algorithm | conv. time (ms) | rounds | messages | bytes | path-cost agreement | loops |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in (r for r in summary if (r["suite"], r["topology"], r["nodes"]) == key):
            f = lambda m: f"{r[m + '_mean']:.1f} ± {r[m + '_std']:.1f}"
            lines.append(f"| {r['condition']} | {r['algorithm']} | {f('convergence_ms')} | {f('rounds')} | "
                         f"{f('messages')} | {f('bytes')} | {r['cost_agreement_mean'] * 100:.1f}% | {r['loops_mean']:.1f} |")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", choices=["random", "six", "all"], default="all")
    parser.add_argument("--sizes", type=int, nargs="+", default=[10, 50, 200])
    parser.add_argument("--algorithms", nargs="+", default=ALGORITHMS, choices=ALGORITHMS)
    parser.add_argument("--jobs", type=int, default=1, help="parallel worker processes")
    parser.add_argument("--output", default="results")
    args = parser.parse_args()

    tasks = make_tasks(args)
    print(f"{len(tasks)} simulations ({args.suite} suite)...")
    started = time.time()
    rows: list[dict] = []
    if args.jobs > 1:
        with Pool(args.jobs) as pool:
            for i, result in enumerate(pool.imap(run_task, tasks), 1):
                rows += result
                print(f"\r{i}/{len(tasks)}", end="", flush=True)
    else:
        for i, task in enumerate(tasks, 1):
            rows += run_task(task)
            print(f"\r{i}/{len(tasks)}", end="", flush=True)
    print(f"\ndone in {time.time() - started:.0f}s")

    rows = dedupe_stable(rows)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "raw.csv", rows, RAW_FIELDS)
    summary = summarize(rows)
    write_csv(out / "summary.csv", summary)
    write_markdown(summary, out / "summary.md")
    print(f"Wrote raw.csv, summary.csv, summary.md in {out}/")


if __name__ == "__main__":
    main()
