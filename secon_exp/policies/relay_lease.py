from .base import PolicySpec

# Ablation: readiness ordering + outstanding-demand-aware relay lease.
POLICY = PolicySpec(
    name="l_lease",
    source_mode="p2p",
    ordering="local",
    coalesce=True,
    eviction="lease",
    lease_rho=0.50,
)
