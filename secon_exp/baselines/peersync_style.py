from secon_exp.policies.base import PolicySpec

# PeerSync-style idea-level reproduction.
#
# Shared ideas:
# - P2P / Registry hybrid delivery;
# - network-position-aware peer selection;
# - replica-aware preservation;
# - cloud fallback.
#
# The unified simulator intentionally does not model
# PeerSync's complete protocol stack, block-level transfer,
# tracker/DHT overhead, or verification protocol.
#
# Therefore this MUST be reported as a PeerSync-inspired
# idea-level reproduction rather than the official system.
POLICY = PolicySpec(
    name="peersync_style",
    source_mode="peersync",
    ordering="fifo",
    coalesce=True,
    eviction="replica_popularity",
)
