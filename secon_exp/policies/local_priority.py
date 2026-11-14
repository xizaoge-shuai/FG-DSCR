from .base import PolicySpec

# Ablation: readiness-aware layer ordering, but ordinary LRU retention.
POLICY = PolicySpec(
    name="l_lru",
    source_mode="p2p",
    ordering="local",
    coalesce=True,
    eviction="lru",
)
