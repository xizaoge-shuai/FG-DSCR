from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PolicySpec:
    name: str

    # cloud | p2p
    source_mode: str

    # fifo | sjf | local | debt
    ordering: str

    # Suppress a new cloud pull while the same layer is already in flight.
    coalesce: bool = False

    # lru | lease
    eviction: str = "lru"

    # Fraction of cache that may be protected by outstanding-demand leases.
    lease_rho: float = 0.0

    # Penalty applied to predicted eviction-induced communication debt.
    debt_lambda: float = 0.0

    def __post_init__(self):
        if self.source_mode not in ("cloud", "p2p"):
            raise ValueError(self.source_mode)

        if self.ordering not in ("fifo", "sjf", "local", "debt"):
            raise ValueError(self.ordering)

        if self.eviction not in ("lru", "lease"):
            raise ValueError(self.eviction)

        if not 0.0 <= self.lease_rho <= 1.0:
            raise ValueError("lease_rho must be in [0, 1]")

        if self.debt_lambda < 0:
            raise ValueError("debt_lambda must be >= 0")
