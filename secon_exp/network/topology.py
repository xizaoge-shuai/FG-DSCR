from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass
class Link:
    lid: str
    capacity_mb_s: float
    latency_ms: float


class EdgeTopology:
    """
    Registry
        |
       WAN
        |
       Core
      / |  \
    GW0 GW1 GW2 ...
     |   |   |
    Edge nodes
    """

    def __init__(
        self,
        nodes,
        num_domains,
        wan_capacity=100.0,
        core_capacity=1000.0,
        domain_capacity=300.0,
        access_capacity=200.0,
        peer_upload_capacity=100.0,
        registry_latency_ms=40.0,
        inter_domain_latency_ms=10.0,
        intra_domain_latency_ms=2.0,
    ):
        self.nodes = {
            n["eid"]: n
            for n in nodes
        }

        self.num_domains = int(num_domains)

        self.domain_of: Dict[str, int] = {}

        for i, eid in enumerate(
            sorted(self.nodes)
        ):
            node = self.nodes[eid]

            self.domain_of[eid] = int(
                node.get(
                    "domain",
                    i % self.num_domains,
                )
            )

        self.capacity = {}
        self.latency = {}

        self._add(
            "wan",
            wan_capacity,
            registry_latency_ms / 2.0,
        )

        self._add(
            "core",
            core_capacity,
            inter_domain_latency_ms / 2.0,
        )

        for d in range(self.num_domains):
            self._add(
                f"domain:{d}",
                domain_capacity,
                inter_domain_latency_ms / 4.0,
            )

        for eid, node in self.nodes.items():
            self._add(
                f"access:{eid}",
                access_capacity,
                intra_domain_latency_ms / 2.0,
            )

            self._add(
                f"tx:{eid}",
                peer_upload_capacity,
                0.0,
            )

            self._add(
                f"rx:{eid}",
                float(
                    node.get(
                        "bandwidth_mb_s",
                        access_capacity,
                    )
                ),
                0.0,
            )

    def _add(
        self,
        name,
        capacity,
        latency,
    ):
        self.capacity[name] = float(capacity)
        self.latency[name] = float(latency)

    def registry_path(
        self,
        dst,
    ) -> Tuple[str, ...]:

        d = self.domain_of[dst]

        return (
            "wan",
            "core",
            f"domain:{d}",
            f"access:{dst}",
            f"rx:{dst}",
        )

    def peer_path(
        self,
        src,
        dst,
    ) -> Tuple[str, ...]:

        sd = self.domain_of[src]
        dd = self.domain_of[dst]

        if sd == dd:
            return (
                f"tx:{src}",
                f"access:{src}",
                f"domain:{sd}",
                f"access:{dst}",
                f"rx:{dst}",
            )

        return (
            f"tx:{src}",
            f"access:{src}",
            f"domain:{sd}",
            "core",
            f"domain:{dd}",
            f"access:{dst}",
            f"rx:{dst}",
        )

    def path_latency_ms(
        self,
        path,
    ):
        return sum(
            self.latency[x]
            for x in path
        )

    def same_domain(
        self,
        a,
        b,
    ):
        return (
            self.domain_of[a]
            == self.domain_of[b]
        )
