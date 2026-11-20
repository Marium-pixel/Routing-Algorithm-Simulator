"""Generate the containerlab topology and FRR configs for the six-router lab.

    python frr/generate_lab.py --protocol both            # default timers
    python frr/generate_lab.py --protocol ospf --tuned    # hello 1 / dead 4 (RIP: 5/15/10)
    python frr/generate_lab.py --protocol rip --rip-split-horizon poisoned

Output: frr/lab/<protocol>/{<protocol>.clab.yml, R1..R6/{daemons,frr.conf}}
"""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from frr.lab_model import DEFAULT_TOPOLOGY, Lab, build
except ImportError:  # executed as a plain script: python frr/generate_lab.py
    from lab_model import DEFAULT_TOPOLOGY, Lab, build

OUT = Path(__file__).resolve().parent / "lab"
FRR_IMAGE = "frrouting/frr:v8.4.1"
HOST_IMAGE = "ghcr.io/hellt/network-multitool"
DAEMON_NAMES = ["bgpd", "ospfd", "ospf6d", "ripd", "ripngd", "isisd", "pimd", "ldpd", "nhrpd", "eigrpd",
                "babeld", "sharpd", "pbrd", "bfdd", "fabricd", "vrrpd", "pathd"]


def daemons(protocol: str) -> str:
    lines = ["zebra=yes"]
    for name in DAEMON_NAMES:
        lines.append(f"{name}={'yes' if name == {'ospf': 'ospfd', 'rip': 'ripd'}[protocol] else 'no'}")
    lines += ['', 'vtysh_enable=yes', 'zebra_options="  -A 127.0.0.1 -s 90000000"',
              'ospfd_options="  -A 127.0.0.1"', 'ripd_options="  -A 127.0.0.1"', '']
    return "\n".join(lines)


def frr_conf(lab: Lab, router: str, protocol: str, tuned: bool, rip_sh: str, unit_ospf_cost: bool) -> str:
    n = router[1:]
    out = [f"hostname {router}", "service integrated-vtysh-config", "!",
           "interface lo", f" ip address {lab.loopbacks[router]}/32", "!"]
    ifaces = lab.ifaces[router]
    for iface in ifaces:
        out += [f"interface {iface.name}", f" ip address {iface.ip}/{iface.prefixlen}"]
        if protocol == "ospf" and iface.kind == "p2p":
            out += [" ip ospf network point-to-point",
                    f" ip ospf cost {1 if unit_ospf_cost else iface.cost}",
                    f" ip ospf hello-interval {1 if tuned else 10}",
                    f" ip ospf dead-interval {4 if tuned else 40}"]
        if protocol == "rip" and iface.kind == "p2p":
            if rip_sh == "poisoned":
                out.append(" ip split-horizon poisoned-reverse")
            elif rip_sh == "none":
                out.append(" no ip split-horizon")
        out.append("!")
    lan = [i for i in ifaces if i.kind == "lan"]
    if protocol == "ospf":
        out += ["router ospf", f" ospf router-id {lab.loopbacks[router]}", " passive-interface lo"]
        out += [f" passive-interface {i.name}" for i in lan]
        out.append(f" network {lab.loopbacks[router]}/32 area 0")
        out += [f" network {i.network} area 0" for i in ifaces]
    else:
        out += ["router rip", " version 2", f" timers basic {5 if tuned else 30} {15 if tuned else 180} {10 if tuned else 120}",
                " network 10.0.0.0/8"]
        out += [f" network {i.network}" for i in lan]
        out.append(" passive-interface lo")
        out += [f" passive-interface {i.name}" for i in lan]
    out += ["!", "line vty", "!", ""]
    return "\n".join(out)


def clab_yaml(lab: Lab, protocol: str, image: str) -> str:
    lines = [f"name: frr-{protocol}", "topology:", "  nodes:"]
    for router in lab.routers:
        lines += [f"    {router}:", "      kind: linux", f"      image: {image}", "      binds:",
                  f"        - {router}/daemons:/etc/frr/daemons", f"        - {router}/frr.conf:/etc/frr/frr.conf"]
    for host, h in lab.hosts.items():
        lines += [f"    {host}:", "      kind: linux", f"      image: {HOST_IMAGE}", "      exec:",
                  f"        - ip addr add {h['ip']}/{h['prefixlen']} dev eth1",
                  f"        - ip route replace default via {h['gateway']}"]
    lines.append("  links:")
    for link in lab.links:
        lines.append(f'    - endpoints: ["{link["a"]}:{link["a_if"]}", "{link["b"]}:{link["b_if"]}"]')
    for host, h in lab.hosts.items():
        lines.append(f'    - endpoints: ["{h["router"]}:{h["router_if"]}", "{host}:eth1"]')
    return "\n".join(lines) + "\n"


def generate(protocol: str, topology=DEFAULT_TOPOLOGY, tuned=False, rip_sh="simple", image=FRR_IMAGE,
             out_dir: Path = OUT, unit_ospf_cost=False) -> Path:
    lab = build(topology)
    base = out_dir / protocol
    for router in lab.routers:
        (base / router).mkdir(parents=True, exist_ok=True)
        (base / router / "daemons").write_text(daemons(protocol), encoding="utf-8", newline="\n")
        (base / router / "frr.conf").write_text(frr_conf(lab, router, protocol, tuned, rip_sh, unit_ospf_cost),
                                                encoding="utf-8", newline="\n")
    topo_file = base / f"{protocol}.clab.yml"
    topo_file.write_text(clab_yaml(lab, protocol, image), encoding="utf-8", newline="\n")
    return topo_file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--protocol", choices=["ospf", "rip", "both"], default="both")
    parser.add_argument("--topology", default=str(DEFAULT_TOPOLOGY))
    parser.add_argument("--tuned", action="store_true", help="aggressive timers (OSPF 1/4 s, RIP 5/15/10 s)")
    parser.add_argument("--rip-split-horizon", choices=["simple", "poisoned", "none"], default="simple")
    parser.add_argument("--image", default=FRR_IMAGE)
    parser.add_argument("--unit-ospf-cost", action="store_true", help="OSPF cost 1 on every link")
    args = parser.parse_args()
    for protocol in (["ospf", "rip"] if args.protocol == "both" else [args.protocol]):
        path = generate(protocol, args.topology, args.tuned, args.rip_split_horizon, args.image,
                        unit_ospf_cost=args.unit_ospf_cost)
        print(f"generated {path}")


if __name__ == "__main__":
    main()
