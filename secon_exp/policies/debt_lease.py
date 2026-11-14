from .base import PolicySpec

# Proposed method.
#
# Fixed globally for formal experiments:
#   rho    = 0.50
#   lambda = 1.00
#
# Do not tune these separately for each workload.
POLICY = PolicySpec(
    name="d_lease",
    source_mode="p2p",
    ordering="debt",
    coalesce=True,
    eviction="lease",
    lease_rho=0.50,
    debt_lambda=1.00,
)
