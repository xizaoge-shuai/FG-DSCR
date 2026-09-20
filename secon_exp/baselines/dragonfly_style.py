from secon_exp.policies.base import PolicySpec

# Dragonfly-style idea-level reproduction:
#
# - P2P 优先；
# - 根据实时链路能力和 peer 上传负载选择 parent；
# - peer 不可用时 back-to-source；
# - 同层正在回源时进行合并；
# - 不加入 CIDER 自己的 layer utility / KEEP。
POLICY = PolicySpec(
    name="dragonfly_style",
    source_mode="dragonfly",
    ordering="fifo",
    coalesce=True,
    eviction="lru",
)
