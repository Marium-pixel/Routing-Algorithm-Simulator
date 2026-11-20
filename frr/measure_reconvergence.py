"""Measure FRR reconvergence (OSPF / RIP) on the six-router lab, with timestamps.

For each run: wait for a stable network, start a timestamped ping H1 -> H2 (ping -D, 0.2 s
interval), apply the event, and record

    outage_s         longest gap between consecutive ping replies around the event
                     (link failure until connectivity restored)
    first_reply_s    time from the event until the first reply after it
    lost_packets     ping requests that got no reply
    route_change_s   time until R1's route to LAN B (192.168.2.0/24) changed (metric or
                     next hops), polled every ~0.1 s via vtysh

Scenarios: fail_r1_r2, fail_r2_r3, cost_r2_r3 (OSPF only: cost of R2's interface toward R3 -> 10).
After each run the lab is restored (link up / cost back) and the script waits for the
baseline route to return.

    python frr/measure_reconvergence.py --protocol ospf --runs 3
    python frr/measure_reconvergence.py --protocol rip  --runs 3 --scenarios fail_r1_r2 fail_r2_r3
    python frr/measure_reconvergence.py --protocol ospf --runs 3 --label tuned     # after deploy --tuned

Writes results/frr/<protocol>_reconvergence_<label>.csv and prints mean / std.
Real-router convergence (timers, SPF delay, kernel/FIB programming) is not equivalent to the
simulator's rounds; compare orders of magnitude and trends, not exact numbers.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from frrctl import LAB, ROOT, dexec, netns_exec, vtysh  # noqa: E402

LAN_B = LAB.hosts["H2"]["network"]
H2_IP = LAB.hosts["H2"]["ip"]
SCENARIOS = {
    "fail_r1_r2": {"kind": "link", "router": "R1", "peer": "R2"},
    "fail_r2_r3": {"kind": "link", "router": "R2", "peer": "R3"},
    "cost_r2_r3": {"kind": "cost", "router": "R2", "peer": "R3", "cost": 10},
}
PING_RE = re.compile(r"\[(?P<ts>\d+\.\d+)\].*icmp_seq=(?P<seq>\d+)")


def route_signature(protocol: str) -> tuple:
    try:
        data = json.loads(vtysh(protocol, "R1", f"show ip route {LAN_B} json"))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return ("none",)
    entries = [e for e in data.get(LAN_B, []) if e.get("protocol") == protocol]
    if not entries:
        return ("none",)
    e = entries[0]
    return (e["metric"], tuple(sorted(nh.get("ip", "") for nh in e.get("nexthops", []))))


def apply(protocol: str, scenario: dict, restore: bool = False) -> None:
    iface = LAB.iface_toward(scenario["router"], scenario["peer"]).name
    if scenario["kind"] == "link":
        dexec(protocol, scenario["router"], f"ip link set {iface} {'up' if restore else 'down'}")
    else:
        original = LAB.iface_toward(scenario["router"], scenario["peer"]).cost
        cost = original if restore else scenario["cost"]
        subprocess.run(["docker", "exec", f"clab-frr-{protocol}-{scenario['router']}", "vtysh", "-c", "configure terminal",
                        "-c", f"interface {iface}", "-c", f"ip ospf cost {cost}"], check=True, capture_output=True)


def wait_for(protocol: str, baseline: tuple, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if route_signature(protocol) == baseline:
            ping = netns_exec(protocol, "H1", ["ping", "-c", "2", "-W", "1", H2_IP], check=False, capture=True)
            if ping.returncode == 0:
                return True
        time.sleep(1)
    return False


def parse_ping(path: Path, t_event: float) -> dict:
    replies = [(int(m["seq"]), float(m["ts"])) for m in map(PING_RE.search, path.read_text().splitlines()) if m]
    if not replies:
        return {"outage_s": float("nan"), "first_reply_s": float("nan"), "lost_packets": -1}
    replies.sort()
    # An outage is a run of lost packets (icmp_seq jump) whose end lies after the event.
    outage = max((b[1] - a[1] for a, b in zip(replies, replies[1:]) if b[0] - a[0] > 1 and b[1] >= t_event), default=0.0)
    after = [ts for _, ts in replies if ts >= t_event]
    sent = replies[-1][0] - replies[0][0] + 1
    return {"outage_s": round(outage, 3), "first_reply_s": round(after[0] - t_event, 3) if after else float("nan"),
            "lost_packets": sent - len(replies)}


def one_run(protocol: str, name: str, scenario: dict, baseline: tuple, duration: int, workdir: Path) -> dict:
    interval = 0.2
    local = workdir / f"{protocol}_{name}_ping.log"
    with local.open("w") as log:  # iputils ping inside H1's namespace: -D = unix timestamps, 0.2 s interval
        ping = subprocess.Popen(["sudo", "ip", "netns", "exec", f"clab-frr-{protocol}-H1", "ping", "-D", "-n",
                                 "-i", str(interval), "-c", str(int(duration / interval)), H2_IP], stdout=log)
    time.sleep(4)  # steady-state reference before the event
    t_event = time.time()
    apply(protocol, scenario)
    change_s = float("nan")
    while time.time() - t_event < duration - 6:
        if route_signature(protocol) != baseline:
            change_s = round(time.time() - t_event, 3)
            break
        time.sleep(0.1)
    ping.wait()
    result = {"protocol": protocol, "scenario": name, "route_change_s": change_s, **parse_ping(local, t_event)}
    apply(protocol, scenario, restore=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--protocol", choices=["ospf", "rip"], required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--scenarios", nargs="+", default=None, choices=list(SCENARIOS))
    parser.add_argument("--duration", type=int, default=None, help="seconds of ping per run (default: 60 OSPF/RIP default timers, 30 tuned)")
    parser.add_argument("--label", default="default", help="free text, e.g. default or tuned (timers)")
    args = parser.parse_args()
    scenarios = args.scenarios or (["fail_r1_r2", "fail_r2_r3", "cost_r2_r3"] if args.protocol == "ospf" else ["fail_r1_r2", "fail_r2_r3"])
    if args.protocol == "rip" and "cost_r2_r3" in scenarios:
        sys.exit("cost_r2_r3 is OSPF-only")
    duration = args.duration or (30 if args.label == "tuned" else 70)
    out_dir = ROOT / "results" / "frr"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Waiting for the initial network to converge ...")
    deadline = time.time() + 180
    baseline = route_signature(args.protocol)
    while baseline == ("none",) and time.time() < deadline:
        time.sleep(2)
        baseline = route_signature(args.protocol)
    if baseline == ("none",):
        sys.exit("R1 has no route to LAN B; is the lab deployed and converged?")
    rows = []
    for name in scenarios:
        for run in range(1, args.runs + 1):
            if not wait_for(args.protocol, baseline, 240):
                sys.exit("network did not return to the baseline route; check the lab")
            print(f"{args.protocol} {name} run {run}/{args.runs} ...", flush=True)
            row = one_run(args.protocol, name, SCENARIOS[name], baseline, duration, out_dir)
            row.update(run=run, label=args.label)
            rows.append(row)
            print("   ", {k: row[k] for k in ("outage_s", "first_reply_s", "lost_packets", "route_change_s")})
    path = out_dir / f"{args.protocol}_reconvergence_{args.label}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["protocol", "label", "scenario", "run", "outage_s", "first_reply_s", "lost_packets", "route_change_s"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nwrote {path}\n{'scenario':<12} {'metric':<15} mean ± std (n)")
    for name in scenarios:
        for metric in ("outage_s", "first_reply_s", "lost_packets", "route_change_s"):
            vals = [r[metric] for r in rows if r["scenario"] == name and r[metric] == r[metric]]
            if vals:
                sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
                print(f"{name:<12} {metric:<15} {statistics.mean(vals):.3f} ± {sd:.3f} ({len(vals)})")


if __name__ == "__main__":
    main()
