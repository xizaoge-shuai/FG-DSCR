from secon_exp.policies.base import PolicySpec

# PeerSync-inspired idea-level reproduction:
#
# - Prefer replicas within the same edge domain / LAN.
# - For non-local retrieval, use network-aware source selection
#   and allow fallback to the upstream Registry.
# - Tiny layers may be fetched directly from the Registry.
# - Cache replacement uses replica/popularity-aware retention.
# - Same destination-layer transfers are coalesced.
#
# Important:
# The unified simulator operates at image-layer granularity.
# It does NOT reproduce PeerSync's complete block-level
# multi-source downloader, EWMA throughput history,
# tracker/DHT discovery, Merkle verification, or timeout
# protocol.
#
# Therefore this baseline MUST be reported as a
# PeerSync-inspired idea-level reproduction, not as the
# official PeerSync implementation.

POLICY = PolicySpec(
    name="peersync_style",
    source_mode="peersync",
    ordering="fifo",
    coalesce=True,
    eviction="replica_popularity",
)
