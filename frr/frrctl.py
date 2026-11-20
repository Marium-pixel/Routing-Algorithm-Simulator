"""Helper CLI for the FRR/containerlab validation lab (run on the Ubuntu VM).

    python frr/frrctl.py deploy ospf              # generate configs + containerlab deploy
    python frr/frrctl.py tables ospf --tag stable # save `show ip route` (text+json) for R1-R6
    python frr/frrctl.py ping ospf                # H1 -> H2 ping
    python frr/frrctl.py traceroute ospf          # H1 -> H2 traceroute
    python frr/frrctl.py link ospf down R1 R2     # ip link set down on R1's interface toward R2
    python frr/frrctl.py link ospf up R1 R2
    python frr/frrctl.py cost ospf R2 R3 10       # OSPF only: change interface cost on R2 toward R3
    python frr/frrctl.py netem ospf R2 R3 --delay 20ms --loss 2%   # tc netem on R2's interface
    python frr/frrctl.py netem ospf R2 R3 --clear
    python frr/frrctl.py capture ospf R1 eth1 --seconds 60 --out captures/ospf_r1_eth1.pcapng
    python frr/frrctl.py vtysh ospf R1 "show ip ospf neighbor"
    python frr/frrctl.py destroy ospf

Needs: Docker, containerlab (sudo), and for capture/netem the host tools tcpdump, tc (iproute2),
and optionally editcap (Wireshark) to convert .pcap -> .pcapng.
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from frr.generate_lab import generate
    from frr.lab_model import build
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from generate_lab import generate
    from lab_model import build

ROOT = Path(__file__).resolve().parent.parent
LAB_DIR = ROOT / "frr" / "lab"
LAB = build()


def container(protocol: str, node: str) -> str:
    return f"clab-frr-{protocol}-{node}"


def topo_file(protocol: str) -> Path:
    return LAB_DIR / protocol / f"{protocol}.clab.yml"


def sh(cmd: list[str], check: bool = True, capture: bool = False, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=capture, timeout=timeout)


def dexec(protocol: str, node: str, command: str, check: bool = True, timeout: float | None = None) -> str:
    result = sh(["docker", "exec", container(protocol, node), "sh", "-c", command], check=check, capture=True, timeout=timeout)
    return result.stdout


def vtysh(protocol: str, node: str, command: str, check: bool = True) -> str:
    return dexec(protocol, node, f"vtysh -c {shlex.quote(command)}", check=check)


def netns_exec(protocol: str, node: str, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    # containerlab exposes every container's network namespace as /var/run/netns/<container name>
    return sh(["sudo", "ip", "netns", "exec", container(protocol, node), *argv], **kwargs)


# ---------------------------------------------------------------- commands
def cmd_generate(a) -> None:
    print(generate(a.protocol, tuned=a.tuned, rip_sh=a.rip_split_horizon))


def cmd_deploy(a) -> None:
    generate(a.protocol, tuned=a.tuned, rip_sh=a.rip_split_horizon)
    sh(["sudo", "containerlab", "deploy", "-t", str(topo_file(a.protocol)), "--reconfigure"])
    time.sleep(3)
    for router in LAB.routers:  # make sure the integrated config has been applied to every daemon
        sh(["docker", "exec", container(a.protocol, router), "vtysh", "-b"], check=False, capture=True)
    print("Deployed. Give the protocol time to converge (RIP default: up to ~30 s, OSPF: ~40 s), then run `tables`.")


def cmd_destroy(a) -> None:
    sh(["sudo", "containerlab", "destroy", "-t", str(topo_file(a.protocol)), "--cleanup"])


def cmd_tables(a) -> None:
    out = ROOT / "results" / "frr" / a.protocol / a.tag
    out.mkdir(parents=True, exist_ok=True)
    extra = {"ospf": ["show ip ospf neighbor", "show ip ospf database"], "rip": ["show ip rip"]}[a.protocol]
    for router in LAB.routers:
        (out / f"{router}.json").write_text(vtysh(a.protocol, router, "show ip route json"), encoding="utf-8")
        text = [f"### {c}\n{vtysh(a.protocol, router, c)}" for c in ["show ip route", *extra]]
        (out / f"{router}.txt").write_text("\n".join(text), encoding="utf-8")
        if a.print:
            print(f"===== {router} =====\n{text[0]}")
    print(f"Saved routing tables to {out}")


def host_cmd(a, argv: list[str], src: str = "H1") -> None:
    # Run the VM's own iputils ping / traceroute inside H1's network namespace (more reliable than
    # the busybox tools in a minimal host image).
    result = netns_exec(a.protocol, src, argv, check=False, capture=True, timeout=120)
    print(result.stdout + result.stderr)


def cmd_ping(a) -> None:
    host_cmd(a, ["ping", "-c", str(a.count), LAB.hosts["H2"]["ip"]])


def cmd_traceroute(a) -> None:
    host_cmd(a, ["traceroute", "-n", LAB.hosts["H2"]["ip"]])


def cmd_vtysh(a) -> None:
    print(vtysh(a.protocol, a.router, a.command))


def cmd_link(a) -> None:
    iface = LAB.iface_toward(a.router, a.peer).name
    dexec(a.protocol, a.router, f"ip link set {iface} {a.state}")
    print(f"{a.router} {iface} (toward {a.peer}) is now {a.state} at {time.time():.6f}")


def cmd_cost(a) -> None:
    if a.protocol != "ospf":
        sys.exit("interface cost change is OSPF-only")
    iface = LAB.iface_toward(a.router, a.peer).name
    sh(["docker", "exec", container(a.protocol, a.router), "vtysh", "-c", "configure terminal",
        "-c", f"interface {iface}", "-c", f"ip ospf cost {a.cost}"])
    print(f"{a.router} {iface} ospf cost -> {a.cost} at {time.time():.6f}")


def cmd_netem(a) -> None:
    iface = LAB.iface_toward(a.router, a.peer).name
    if a.clear:
        netns_exec(a.protocol, a.router, ["tc", "qdisc", "del", "dev", iface, "root"], check=False)
        print(f"cleared netem on {a.router}:{iface}")
        return
    spec = []
    if a.delay:
        spec += ["delay", a.delay]
    if a.loss:
        spec += ["loss", a.loss]
    if not spec:
        sys.exit("give --delay and/or --loss, or --clear")
    netns_exec(a.protocol, a.router, ["tc", "qdisc", "replace", "dev", iface, "root", "netem", *spec])
    print(f"netem {' '.join(spec)} on {a.router}:{iface}")


def cmd_capture(a) -> None:
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = out.with_suffix(".pcap")
    print(f"capturing {a.seconds}s on {a.router}:{a.iface} ...")
    netns_exec(a.protocol, a.router, ["timeout", "-s", "INT", str(a.seconds), "tcpdump", "-i", a.iface, "-n",
                                      "-w", str(raw), *shlex.split(a.filter)], check=False)
    if shutil.which("editcap"):
        sh(["editcap", "-F", "pcapng", str(raw), str(out)])
        raw.unlink()
        print(f"wrote {out}")
    else:
        print(f"editcap not found; wrote classic pcap {raw} (open it in Wireshark and 'Save As' .pcapng)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *args, help=None):
        sp = sub.add_parser(name, help=help)
        sp.add_argument("protocol", choices=["ospf", "rip"])
        for arg in args:
            sp.add_argument(arg)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("generate", cmd_generate)
    sp.add_argument("--tuned", action="store_true")
    sp.add_argument("--rip-split-horizon", choices=["simple", "poisoned", "none"], default="simple")
    sp = add("deploy", cmd_deploy)
    sp.add_argument("--tuned", action="store_true")
    sp.add_argument("--rip-split-horizon", choices=["simple", "poisoned", "none"], default="simple")
    add("destroy", cmd_destroy)
    sp = add("tables", cmd_tables)
    sp.add_argument("--tag", default="stable")
    sp.add_argument("--print", action="store_true", help="also print `show ip route` of every router")
    sp = add("ping", cmd_ping)
    sp.add_argument("--count", type=int, default=5)
    add("traceroute", cmd_traceroute)
    add("vtysh", cmd_vtysh, "router", "command")
    sp = add("link", cmd_link, "state", "router", "peer")
    sp.set_defaults(fn=cmd_link)
    sp = add("cost", cmd_cost, "router", "peer")
    sp.add_argument("cost", type=int)
    sp = add("netem", cmd_netem, "router", "peer")
    sp.add_argument("--delay")
    sp.add_argument("--loss")
    sp.add_argument("--clear", action="store_true")
    sp = add("capture", cmd_capture, "router", "iface")
    sp.add_argument("--seconds", type=int, default=60)
    sp.add_argument("--out", required=True)
    sp.add_argument("--filter", default="", help="optional tcpdump filter, e.g. 'ip proto 89' or 'udp port 520'")
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
