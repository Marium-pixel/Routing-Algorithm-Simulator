"""Unit tests.  Run from the project root:  python -m unittest discover -s tests -v"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simulator import ALGORITHMS, load_events, random_topology, run_simulation, validate_routes
from simulator.engine import EventEngine
from simulator.topology import Link, Topology

EX = Path(__file__).resolve().parent.parent / "examples"


def event(kind, a, b, **extra):
    return {"time_ms": 0, "type": kind, "a": a, "b": b, **extra}


class EngineTests(unittest.TestCase):
    def test_events_run_in_time_then_insertion_order(self):
        engine, seen = EventEngine(), []
        engine.schedule_at(5, seen.append, "b1")
        engine.schedule_at(1, seen.append, "a")
        engine.schedule_at(5, seen.append, "b2")
        engine.run()
        self.assertEqual(seen, ["a", "b1", "b2"])

    def test_cannot_schedule_in_past(self):
        engine = EventEngine()
        engine.schedule_at(3, lambda: None)
        engine.run()
        with self.assertRaises(ValueError):
            engine.schedule_at(1, lambda: None)


class ConvergenceTests(unittest.TestCase):
    def test_six_router_matches_dijkstra_for_every_algorithm(self):
        topo = Topology.from_json(EX / "six_router.json")
        for algorithm in ALGORITHMS:
            sim = run_simulation(topo, algorithm)
            check = validate_routes(sim)
            self.assertEqual(check["cost_ok"], check["pairs"], algorithm)
            self.assertEqual(check["loops"], 0, algorithm)
            self.assertEqual(sim.route_table()["R1"]["R3"], {"cost": 2, "next_hop": "R2"}, algorithm)

    def test_random_graphs_stable(self):
        for seed in (1, 2, 3):
            topo = random_topology(30, seed)
            infinity = 2 * topo.weighted_diameter() + 20
            for algorithm in ALGORITHMS:
                check = validate_routes(run_simulation(topo, algorithm, infinity=infinity))
                self.assertEqual(check["cost_ok"], check["pairs"], (seed, algorithm))

    def test_link_failure_reroutes_with_ls_and_poison_reverse(self):
        topo = Topology.from_json(EX / "six_router.json")
        for algorithm in ("ls", "dv", "dv-poison-reverse"):
            sim = run_simulation(topo, algorithm, [event("link_down", "R2", "R3")])
            check = validate_routes(sim)
            self.assertEqual(check["cost_ok"], check["pairs"], algorithm)
            self.assertEqual(sim.route_table()["R1"]["R3"]["cost"], 6, algorithm)

    def test_cost_change(self):
        topo = Topology.from_json(EX / "six_router.json")
        sim = run_simulation(topo, "ls", [event("cost_change", "R2", "R3", cost=10)])
        self.assertEqual(sim.route_table()["R1"]["R3"]["cost"], 6)

    def test_link_up_resynchronises_after_partition(self):
        # R1-R2-R3 line; cut R2-R3, then restore it: everyone must reach everyone again.
        topo = Topology(["R1", "R2", "R3"], [Link("R1", "R2", 1, 2), Link("R2", "R3", 1, 2)])
        events = [{"time_ms": 0, "type": "link_down", "a": "R2", "b": "R3"},
                  {"time_ms": 500, "type": "link_up", "a": "R2", "b": "R3"}]
        for algorithm in ALGORITHMS:
            sim = run_simulation(topo, algorithm, events)
            check = validate_routes(sim)
            self.assertEqual(check["cost_ok"], check["pairs"], algorithm)

    def test_deterministic(self):
        topo = random_topology(20, 5)
        a = run_simulation(topo, "dv", infinity=100)
        b = run_simulation(topo, "dv", infinity=100)
        self.assertEqual((a.metrics.messages, a.metrics.bytes), (b.metrics.messages, b.metrics.bytes))
        self.assertEqual(a.route_table(), b.route_table())


class CountToInfinityTests(unittest.TestCase):
    def setUp(self):
        self.topo = Topology.from_json(EX / "count_to_infinity.json")
        self.events = load_events(EX / "count_to_infinity_failure.json")

    def phase(self, algorithm):
        return run_simulation(self.topo, algorithm, self.events, 16).get_phase("events")

    def test_plain_dv_counts_to_infinity(self):
        self.assertGreaterEqual(self.phase("dv").rounds, 14)

    def test_split_horizon_and_poison_reverse_avoid_it(self):
        for algorithm in ("dv-split-horizon", "dv-poison-reverse"):
            self.assertLessEqual(self.phase(algorithm).rounds, 3, algorithm)

    def test_final_state_is_unreachable(self):
        sim = run_simulation(self.topo, "dv", self.events, 16)
        self.assertEqual(sim.route_table()["X"]["Z"]["cost"], 16)
        self.assertIsNone(sim.route_table()["X"]["Z"]["next_hop"])

    def test_poison_reverse_beats_plain_on_textbook_cost_increase(self):
        topo = Topology.from_json(EX / "triangle_cost_increase.json")
        events = load_events(EX / "triangle_cost_increase_event.json")
        plain = run_simulation(topo, "dv", events, 100).get_phase("events")
        poison = run_simulation(topo, "dv-poison-reverse", events, 100).get_phase("events")
        self.assertGreater(plain.rounds, 20)
        self.assertLess(poison.rounds, 6)


class AccountingTests(unittest.TestCase):
    def test_phase_message_totals_match_global(self):
        sim = run_simulation(Topology.from_json(EX / "six_router.json"), "ls", [event("link_down", "R1", "R2")])
        self.assertEqual(sum(p.messages for p in sim.phases), sim.metrics.messages)
        self.assertEqual(sum(p.bytes for p in sim.phases), sim.metrics.bytes)

    def test_no_events_means_only_initial_phase(self):
        sim = run_simulation(Topology.from_json(EX / "six_router.json"), "dv")
        self.assertEqual([p.name for p in sim.phases], ["initial"])


if __name__ == "__main__":
    unittest.main()
