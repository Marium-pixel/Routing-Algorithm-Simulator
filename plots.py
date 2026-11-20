"""Create report figures from results/*.csv (run experiments.py and count_to_infinity.py first).

    python plots.py --results results --output results/figures
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx

from simulator.topology import Topology

COLORS = {"ls": "#0072B2", "dv": "#D55E00", "dv-split-horizon": "#009E73", "dv-poison-reverse": "#CC79A7"}
MARKERS = {"ls": "o", "dv": "s", "dv-split-horizon": "^", "dv-poison-reverse": "D"}
CONDITIONS = ["stable", "cost_change", "link_failure"]
LABELS = {"convergence_ms": "Convergence time (simulated ms)", "rounds": "Update rounds",
          "messages": "Routing messages", "bytes": "Routing bytes"}


def read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def scalability(summary: list[dict], out: Path) -> None:
    rows = [r for r in summary if r["suite"] == "random"]
    if not rows:
        return
    for metric, label in LABELS.items():
        fig, axes = plt.subplots(1, 3, figsize=(13, 3.8), sharey=False)
        for ax, condition in zip(axes, CONDITIONS):
            for algorithm, color in COLORS.items():
                pts = sorted((int(r["nodes"]), float(r[f"{metric}_mean"]), float(r[f"{metric}_std"]))
                             for r in rows if r["algorithm"] == algorithm and r["condition"] == condition)
                if not pts:
                    continue
                x, y, e = zip(*pts)
                ax.errorbar(x, y, yerr=e, label=algorithm, color=color, marker=MARKERS[algorithm], capsize=3)
            ax.set_xscale("log")
            if metric in ("messages", "bytes"):
                ax.set_yscale("log")
            ax.set_xticks(sorted({int(r["nodes"]) for r in rows}))
            ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
            ax.set_title(condition.replace("_", " "))
            ax.set_xlabel("Nodes")
            ax.grid(alpha=0.3)
        axes[0].set_ylabel(label)
        axes[0].legend(fontsize=8)
        fig.suptitle(f"{label} vs network size (mean ± std, 3 seeds)")
        fig.tight_layout()
        fig.savefig(out / f"scalability_{metric}.png", dpi=150)
        plt.close(fig)


def count_to_infinity(trace: list[dict], out: Path) -> None:
    scenarios = list(dict.fromkeys(r["scenario"] for r in trace))
    if not scenarios:
        return
    fig, axes = plt.subplots(1, len(scenarios), figsize=(5.2 * len(scenarios), 3.8))
    axes = [axes] if len(scenarios) == 1 else list(axes)
    for ax, scenario in zip(axes, scenarios):
        series = defaultdict(list)
        for r in trace:
            if r["scenario"] == scenario:
                series[(r["algorithm"], r["node"])].append((int(r["time_ms"]), int(r["cost"])))
        for (algorithm, node), pts in sorted(series.items()):
            if algorithm == "ls":
                continue
            pts.sort()
            ax.step([t for t, _ in pts], [c for _, c in pts], where="post", color=COLORS[algorithm],
                    linestyle={"X": "-", "Y": "--", "Z": ":", "A": "-", "B": "--", "C": ":"}.get(node, "-"),
                    label=f"{algorithm} @ {node}")
        ax.set_title(scenario.replace("_", " "))
        ax.set_xlabel("Time after event (ms)")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=7)
    axes[0].set_ylabel("Advertised cost to destination")
    fig.suptitle("Count-to-infinity: cost of the affected route over time")
    fig.tight_layout()
    fig.savefig(out / "count_to_infinity.png", dpi=150)
    plt.close(fig)


def topology_figure(out: Path) -> None:
    path = Path(__file__).parent / "examples" / "six_router.json"
    topo = Topology.from_json(path)
    graph = topo.to_networkx()
    pos = {"R1": (0, 1), "R2": (1, 1), "R3": (2, 1), "R4": (0, 0), "R5": (1, 0), "R6": (2, 0)}
    fig, ax = plt.subplots(figsize=(5, 3.2))
    nx.draw_networkx(graph, pos, ax=ax, node_color="#cfe3f5", node_size=900, edgecolors="#0072B2")
    nx.draw_networkx_edge_labels(graph, pos, ax=ax, edge_labels={(l.a, l.b): l.cost for l in topo.links})
    ax.set_title("Six-router topology (edge labels = OSPF cost)")
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(out / "six_router_topology.png", dpi=150)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results")
    parser.add_argument("--output", default="results/figures")
    args = parser.parse_args()
    results, out = Path(args.results), Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    if (results / "summary.csv").exists():
        scalability(read(results / "summary.csv"), out)
    if (results / "count_to_infinity_trace.csv").exists():
        count_to_infinity(read(results / "count_to_infinity_trace.csv"), out)
    topology_figure(out)
    print(f"Figures written to {out}/")


if __name__ == "__main__":
    main()
