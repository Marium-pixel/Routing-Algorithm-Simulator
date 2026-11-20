"""Single source of truth for the FRR lab: interfaces, addresses, link costs.

Derived from examples/six_router.json so the simulator and the FRR network describe
the *same* topology, following the addressing plan in the project proposal:

* loopback of Rn         10.255.0.n/32
* link i (0-based, in the order listed in the JSON)   10.0.0.(4*i)/30, first router .1, second .2
* LAN A  R1 .1 / H1 .10  192.168.1.0/24      (R1 eth3 <-> H1 eth1)
* LAN B  R3 .1 / H2 .10  192.168.2.0/24      (R3 eth3 <-> H2 eth1)
* interfaces are numbered eth1.. in the order the router appears in the link list; the
  LAN-facing interface comes last.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TOPOLOGY = ROOT / "examples" / "six_router.json"
LANS = {  # router: (host, subnet)
    "R1": ("H1", "192.168.1.0/24"),
    "R3": ("H2", "192.168.2.0/24"),
}


@dataclass
class Iface:
    router: str
    name: str
    ip: str
    prefixlen: int
    network: str
    peer: str            # neighbouring router, or host name for a LAN interface
    cost: int            # OSPF cost (hop count 1 is used for RIP)
    kind: str            # "p2p" or "lan"


@dataclass
class Lab:
    routers: list[str]
    links: list[dict]           # {a, b, cost, network, a_if, b_if, a_ip, b_ip}
    ifaces: dict[str, list[Iface]]
    loopbacks: dict[str, str]
    hosts: dict[str, dict]      # name -> {ip, prefixlen, gateway, router, router_if, network}

    def iface_toward(self, router: str, peer: str) -> Iface:
        for iface in self.ifaces[router]:
            if iface.peer == peer:
                return iface
        raise KeyError(f"{router} has no interface toward {peer}")

    def ip_owner(self) -> dict[str, str]:
        """interface IP -> router name (used to turn FRR next-hop IPs into routers)."""
        return {i.ip: r for r, items in self.ifaces.items() for i in items}


def build(topology_path: str | Path = DEFAULT_TOPOLOGY) -> Lab:
    data = json.loads(Path(topology_path).read_text(encoding="utf-8"))
    routers = list(data["nodes"])
    ifaces: dict[str, list[Iface]] = {r: [] for r in routers}
    links = []
    for index, item in enumerate(data["links"]):
        network = ipaddress.ip_network(f"10.0.0.{4 * index}/30")
        a_ip, b_ip = str(network[1]), str(network[2])
        a_if, b_if = f"eth{len(ifaces[item['a']]) + 1}", f"eth{len(ifaces[item['b']]) + 1}"
        cost = int(item.get("cost", 1))
        ifaces[item["a"]].append(Iface(item["a"], a_if, a_ip, 30, str(network), item["b"], cost, "p2p"))
        ifaces[item["b"]].append(Iface(item["b"], b_if, b_ip, 30, str(network), item["a"], cost, "p2p"))
        links.append({"a": item["a"], "b": item["b"], "cost": cost, "network": str(network),
                      "a_if": a_if, "b_if": b_if, "a_ip": a_ip, "b_ip": b_ip})
    hosts = {}
    for router, (host, subnet) in LANS.items():
        network = ipaddress.ip_network(subnet)
        name = f"eth{len(ifaces[router]) + 1}"
        ifaces[router].append(Iface(router, name, str(network[1]), network.prefixlen, str(network), host, 1, "lan"))
        hosts[host] = {"ip": str(network[10]), "prefixlen": network.prefixlen, "gateway": str(network[1]),
                       "router": router, "router_if": name, "network": str(network)}
    loopbacks = {r: f"10.255.0.{int(r[1:])}" for r in routers}
    return Lab(routers, links, ifaces, loopbacks, hosts)
