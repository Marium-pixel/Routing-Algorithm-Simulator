"""Tests for the FRR toolkit that need no containers: config generation and the
simulator-vs-FRR comparison parser (fed with synthetic `show ip route json` output)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "frr"))

import networkx as nx

from compare_with_simulator import compare, parse_frr_route  # noqa: E402
from generate_lab import generate  # noqa: E402
from lab_model import build  # noqa: E402
from simulator.topology import Topology  # noqa: E402

TOPO = ROOT / "examples" / "six_router.json"


def synthetic_frr_tables(protocol: str, out: Path, down=()) -> None:
    """Write what FRR would report if it agreed with Dijkstra (+ RIP's +1 metric offset)."""
    lab = build(TOPO)
    topo = Topology.from_json(TOPO)
    if protocol == "rip":
        topo = topo.unit_cost()
    for pair in down:
        a, b = pair.split("-")
        topo.link(a, b).up = False
    graph = topo.to_networkx()
    out.mkdir(parents=True, exist_ok=True)
    for src in lab.routers:
        dist = nx.single_source_dijkstra_path_length(graph, src, weight="weight")
        table = {}
        for dst in lab.routers:
            if dst == src or dst not in dist:
                continue
            dist_to_dst = nx.single_source_dijkstra_path_length(graph, dst, weight="weight")
            hops = [n for n in graph.neighbors(src) if graph[src][n]["weight"] + dist_to_dst[n] == dist[dst]]
            prefix = f"{lab.loopbacks[dst]}/32"
            table[prefix] = [{"prefix": prefix, "protocol": protocol, "selected": True,
                              "metric": dist[dst] + (1 if protocol == "rip" else 0),
                              "nexthops": [{"ip": lab.iface_toward(h, src).ip, "interfaceName": "ethX"} for h in hops]}]
        (out / f"{src}.json").write_text(json.dumps(table))


class GeneratorTests(unittest.TestCase):
    def test_addressing_plan_matches_proposal(self):
        lab = build(TOPO)
        self.assertEqual(lab.loopbacks["R6"], "10.255.0.6")
        expected = {("R1", "R2"): ("10.0.0.0/30", "10.0.0.1", "10.0.0.2"), ("R2", "R3"): ("10.0.0.4/30", "10.0.0.5", "10.0.0.6"),
                    ("R1", "R4"): ("10.0.0.8/30", "10.0.0.9", "10.0.0.10"), ("R2", "R5"): ("10.0.0.12/30", "10.0.0.13", "10.0.0.14"),
                    ("R3", "R6"): ("10.0.0.16/30", "10.0.0.17", "10.0.0.18"), ("R4", "R5"): ("10.0.0.20/30", "10.0.0.21", "10.0.0.22"),
                    ("R5", "R6"): ("10.0.0.24/30", "10.0.0.25", "10.0.0.26")}
        for link in lab.links:
            self.assertEqual((link["network"], link["a_ip"], link["b_ip"]), expected[(link["a"], link["b"])])
        self.assertEqual(lab.hosts["H1"], {"ip": "192.168.1.10", "prefixlen": 24, "gateway": "192.168.1.1", "router": "R1",
                                           "router_if": "eth3", "network": "192.168.1.0/24"})
        self.assertEqual(lab.hosts["H2"]["gateway"], "192.168.2.1")

    def test_six_routers_nine_subnets(self):
        lab = build(TOPO)
        subnets = {i.network for items in lab.ifaces.values() for i in items}
        self.assertEqual(len(lab.routers), 6)
        self.assertEqual(len(subnets), 9)  # 7 point-to-point + 2 LANs

    def test_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            ospf = generate("ospf", out_dir=Path(tmp))
            rip = generate("rip", out_dir=Path(tmp), tuned=True, rip_sh="poisoned")
            conf = (ospf.parent / "R2" / "frr.conf").read_text()
            self.assertIn("ospf router-id 10.255.0.2", conf)
            self.assertIn("ip ospf cost 2", conf)       # R2-R5 has cost 2
            self.assertIn("ip ospf hello-interval 10", conf)
            self.assertIn("ospfd=yes", (ospf.parent / "R2" / "daemons").read_text())
            rconf = (rip.parent / "R1" / "frr.conf").read_text()
            self.assertIn("timers basic 5 15 10", rconf)
            self.assertIn("ip split-horizon poisoned-reverse", rconf)
            self.assertIn("ripd=yes", (rip.parent / "R1" / "daemons").read_text())
            self.assertIn('"R1:eth3", "H1:eth1"', ospf.read_text())


class CompareTests(unittest.TestCase):
    def check(self, protocol, down=()):
        with tempfile.TemporaryDirectory() as tmp:
            synthetic_frr_tables(protocol, Path(tmp), down)
            for algorithm in ("ls", "dv-poison-reverse"):
                rows = compare(protocol, Path(tmp), TOPO, list(down), algorithm)
                self.assertEqual(len(rows), 30)
                self.assertEqual(sum(r["cost_match"] for r in rows), 30, (protocol, down, algorithm))
                self.assertEqual(sum(r["next_hop_match"] for r in rows), 30, (protocol, down, algorithm))

    def test_ospf_stable(self):
        self.check("ospf")

    def test_ospf_after_failure(self):
        self.check("ospf", ["R1-R2"])
        self.check("ospf", ["R2-R3"])

    def test_rip_stable_and_failure(self):
        self.check("rip")
        self.check("rip", ["R2-R3"])

    def test_mismatch_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            synthetic_frr_tables("ospf", Path(tmp))
            path = Path(tmp) / "R1.json"
            data = json.loads(path.read_text())
            data["10.255.0.3/32"][0]["metric"] += 5
            path.write_text(json.dumps(data))
            rows = compare("ospf", Path(tmp), TOPO, [], "ls")
            self.assertEqual(sum(r["cost_match"] for r in rows), 29)

    def test_parse_picks_protocol_route(self):
        table = {"10.255.0.2/32": [{"protocol": "connected", "metric": 0, "nexthops": []},
                                   {"protocol": "ospf", "selected": True, "metric": 7, "nexthops": [{"ip": "10.0.0.2"}]}]}
        self.assertEqual(parse_frr_route(table, "10.255.0.2/32", "ospf"), (7, ["10.0.0.2"]))
        self.assertIsNone(parse_frr_route(table, "10.255.0.2/32", "rip"))


if __name__ == "__main__":
    unittest.main()
