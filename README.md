# Routing Algorithm Simulator: Link-State vs Distance-Vector, validated against OSPF and RIP

Course: Computer Networks (CS-3001), Project 18. Team: Marium Imran (24K-0906), Armeen Azam (24K-0654).

* **Part 1 – Python discrete-event simulator** (`simulator/`): every router is an independent agent that
  exchanges messages over links with a configurable delay.
  * **LS**: LSA flooding (sequence numbers, database exchange on link-up, two-way check) + Dijkstra.
  * **DV**: Bellman-Ford with triggered updates: `dv` (plain), `dv-split-horizon`, `dv-poison-reverse`.
  * Events: `cost_change`, `link_down`, `link_up`. Metrics: convergence time, rounds, messages, bytes,
    route changes, path-cost agreement with NetworkX Dijkstra, loop count.
* **Part 2 – FRR validation lab** (`frr/`): six FRRouting routers + 2 hosts in containerlab running OSPFv2 or
  RIPv2, generated from the same `examples/six_router.json` the simulator uses.

## Layout

```
main.py                    run one topology / algorithm / event file
experiments.py             3 conditions x seeds 1-3 x 4 algorithms; random 10/50/200 nodes + six-router
count_to_infinity.py       line, textbook cost-increase and loop-of-three scenarios
plots.py                   figures for the report -> results/figures
simulator/                 engine, topology, protocols (LS/DV), runner (metrics), validate
examples/                  topologies and event files (six_router.json mirrors the FRR lab)
frr/                       lab_model, generate_lab, frrctl, measure_reconvergence, compare_with_simulator
tests/                     python -m unittest discover -s tests -v      (22 tests, no containers needed)
results/                   pre-generated output of the commands below
AI_USE_DISCLOSURE.md       fill in before submission
```

## Part 1: simulator (any OS, Python >= 3.10)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m unittest discover -s tests -v                # sanity check

# POC 1: converged tables for all four algorithms
python main.py examples/six_router.json
# POC 2: cost change (R2-R3 -> 10) and POC 3: link failure (R1-R2)
python main.py examples/six_router.json --events examples/link_cost_change.json --quiet
python main.py examples/six_router.json --events examples/link_failure.json --quiet --output results/six_failure
# POC 4: count-to-infinity X-Y-Z (RIP-compatible infinity = 16)
python main.py examples/count_to_infinity.json --events examples/count_to_infinity_failure.json --quiet
python count_to_infinity.py                            # all three scenarios -> results/count_to_infinity*.csv

# Experiments: ~2 min on 2 cores. Seeds 1,2,3 -> different random topology, costs and delays.
python experiments.py --suite all --jobs 2 --output results
python plots.py --results results --output results/figures
```

Quick run: `python experiments.py --suite all --sizes 10 50`.

**Conventions.** Event `time_ms` values are *relative to the end of initial convergence*. Reported
`convergence_ms` = simulated time from the (first) event to the last routing-table change anywhere;
`quiescence_ms` = until the last message is delivered; `rounds` = longest causal chain of messages;
bytes = size of the JSON encoding of each message (`{"type":..,"payload":..}`). Random-graph runs use
link costs 1-10, delays 1-10 ms, a cost increase of +10 and an infinity of `2*diameter+20` so that weighted
paths are never mistaken for unreachable; six-router/RIP-compatible runs use infinity 16.

### Known modelling limits (state these in the report)

* Triggered updates only; no periodic refresh, hold-down or route timeout (RIP's 30/180 s timers are not modelled).
  Consequently **split horizon alone leaves stale entries** (a neighbour never hears that a route it learned
  from you is gone) and can end with routing loops/mismatches; real RIP would clear them after the 180 s
  timeout. Poisoned reverse sends an explicit infinity and stays correct. This is visible as `loops > 0` /
  path-cost agreement < 100 % for `dv-split-horizon` in `results/summary.md`.
* Poisoned reverse does not stop three-node loops (`loop_of_three` scenario: still 15 rounds).
* No packet loss in the simulator; LS flooding is assumed reliable.
* Simulated rounds/ms are not comparable with real-router wall-clock convergence.

## Part 2: FRR validation (Ubuntu 22.04/24.04 VM)

Prerequisites: Docker Engine, [containerlab](https://containerlab.dev/install/), `tcpdump`, `traceroute`,
`iproute2` (tc), Wireshark (`editcap`/GUI). Run from the project root; commands use `sudo` internally.

```bash
# 1. deploy (generates frr/lab/<proto>/ configs, then containerlab deploy)
python frr/frrctl.py deploy ospf
sleep 60                                              # OSPF ~40 s dead interval; RIP ~30 s update
# 2. routing tables, connectivity, traceroute  (screenshots for the report)
python frr/frrctl.py tables ospf --tag stable --print
python frr/frrctl.py ping ospf
python frr/frrctl.py traceroute ospf                  # H1 -> 192.168.2.10 : R1, R2, R3, H2
# 3. compare with the simulator (OSPF uses the costs in six_router.json; RIP uses hop count, FRR metric = hops+1)
python frr/compare_with_simulator.py --protocol ospf --frr-dir results/frr/ospf/stable --csv results/frr/ospf_stable_compare.csv
# 4. failure: fail, wait, save tables, compare with the simulator run with the same link down
python frr/frrctl.py link ospf down R1 R2 && sleep 15
python frr/frrctl.py tables ospf --tag fail_r1_r2
python frr/compare_with_simulator.py --protocol ospf --frr-dir results/frr/ospf/fail_r1_r2 --down R1-R2
python frr/frrctl.py link ospf up R1 R2
# 5. cost change (OSPF only), impairments, captures
python frr/frrctl.py cost ospf R2 R3 10
python frr/frrctl.py netem ospf R2 R3 --delay 20ms --loss 2%       # --clear to remove
python frr/frrctl.py capture ospf R1 eth1 --seconds 60 --out captures/ospf_r1_eth1.pcapng --filter "ip proto 89"
# 6. timed reconvergence, 3 runs per scenario, mean/std -> results/frr/<proto>_reconvergence_<label>.csv
python frr/measure_reconvergence.py --protocol ospf --runs 3
python frr/frrctl.py destroy ospf
# repeat with rip (capture filter "udp port 520"); optional tuned timers:
python frr/frrctl.py deploy ospf --tuned   &&   python frr/measure_reconvergence.py --protocol ospf --runs 3 --label tuned
```

Addressing (generated by `frr/lab_model.py`, identical to the proposal): loopbacks `10.255.0.n/32`; link *i*
`10.0.0.(4i)/30` (first router .1, second .2: R1-R2 10.0.0.0/30, R2-R3 .4, R1-R4 .8, R2-R5 .12, R3-R6 .16,
R4-R5 .20, R5-R6 .24); LAN A 192.168.1.0/24 (R1 .1, H1 .10); LAN B 192.168.2.0/24 (R3 .1, H2 .10).
Interfaces: R1 eth1→R2, eth2→R4, eth3→H1; R2 eth1→R1, eth2→R3, eth3→R5; R3 eth1→R2, eth2→R6, eth3→H2;
R4 eth1→R1, eth2→R5; R5 eth1→R2, eth2→R4, eth3→R6; R6 eth1→R3, eth2→R5.

### Things to verify on first run (not testable without containers)

* The FRR image tag (`frrouting/frr:v8.4.1`) and host image are pulled; change with
  `python frr/generate_lab.py --image ...`.
* If a router has no routes after deploy, `frrctl deploy` already runs `vtysh -b`; check
  `python frr/frrctl.py vtysh ospf R1 "show running-config"`.
* RIP's metric offset (+1) and OSPF loopback metric 0 are encoded in `compare_with_simulator.py`; the table
  prints raw FRR metrics next to the expected value so any assumption can be checked at a glance.
* ECMP: after R2-R3 fails, R1 has two equal-cost paths (cost 6); the comparison accepts any shortest-path next hop.
* Real-router convergence depends on timers (RIP 30/180 s, OSPF 10/40 s; tuned runs available), SPF delays and
  FIB programming; compare trends, not exact numbers.

## Deliverables still produced by you

Architecture diagram, saved `.pcapng` traces with annotated Wireshark screenshots, FRR routing-table
screenshots/outputs, the 6-10 page report (use `results/summary.md` and `results/figures/`) and the
filled-in `AI_USE_DISCLOSURE.md`.
