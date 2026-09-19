from .base import PolicySpec

# Strong internal baseline:
# P2P source selection + in-flight duplicate suppression + finite-cache LRU.
POLICY = PolicySpec(
    name="c_lru",
    source_mode="p2p",
    ordering="sjf",
    coalesce=True,
    eviction="lru",
)
