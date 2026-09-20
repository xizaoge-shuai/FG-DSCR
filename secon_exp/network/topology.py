from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class Link:
    lid: str
    capacity_mb_s: float
    latency_ms: float


class EdgeTopology:
    """
    Multi-domain edge topology.

    Registry
       |
      WAN
       |
      Core
       |
    Domain gateways
       |
    Edge nodes
    """

    def __init__(
        self,
        nodes: List[dict],
        num_domains: int,
        wan_capacity: float,
        core_capacity: float,
        domain_uplink_capacity: float,
        access_capacity: float,
        peer_upload_capacity: float,
        registry_rtt_ms: float = 40.0,
        inter_domain_rtt_ms: float = 10.0,
        intra_domain_rtt_ms: float = 2.0,
    ):
        if num_domains <= 0:
            raise ValueError("num_domains must be positive")

        self.nodes = {
            x["eid"]: x
            for x in nodes
        }

        self.num_domains = num_domains

        self.domain_of: Dict[str, int] = {}

        for i, eid in enumerate(sorted(self.nodes)):
            explicit = self.nodes[eid].get("domain")

            if explicit is None:
                d = i % num_domains
            else:
                d = int(explicit)

            self.domain_of[eid] = d

        self.caps: Dict[str, float] = {}

        self.latency_ms: Dict[str, float] = {}

        self._add(
            "wan",
            wan_capacity,
            registry_rtt_ms / 2.0,
        )

        self._add(
            "core",
            core_capacity,
            inter_domain_rtt_ms / 2.0,
        )

        for d in range(num_domains):
            self._add(
                f"domain:{d}",
                domain_uplink_capacity,
                inter_domain_rtt_ms / 4.0,
            )

        for eid in self.nodes:
            self._add(
                f"access:{eid}",
                access_capacity,
                intra_domain_rtt_ms / 2.0,
            )

            self._add(
                f"tx:{eid}",
                peer_upload_capacity,
                0.0,
            )

            self._add(
                f"rx:{eid}",
                float(
                    self.nodes[eid].get(
                        "bandwidth_mb_s",
                        access_capacity,
                    )
                ),
                0.0,
            )

    def _add(
        self,
        lid: str,
        capacity: float,
        latency_ms: float,
    ):
        if capacity <= 0:
            raise ValueError(
                f"invalid capacity for {lid}"
            )

        self.caps[lid] = float(capacity)
        self.latency_ms[lid] = float(latency_ms)

    def registry_path(
        self,
        dst: str,
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
        src: str,
        dst: str,
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
        path: Tuple[str, ...],
    ) -> float:

        return sum(
            self.latency_ms[x]
            for x in path
        )

    def same_domain(
        self,
        a: str,
        b: str,
    ) -> bool:

        return (
            self.domain_of[a]
            == self.domain_of[b]
        )
