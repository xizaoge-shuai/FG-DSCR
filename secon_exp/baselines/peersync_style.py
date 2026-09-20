from secon_exp.policies.base import PolicySpec

# PeerSync-style idea-level reproduction:
#
# - P2P / Registry 混合；
# - 网络位置和实时速率感知 peer selection；
# - 内容流行度参与 peer 评价；
# - replica/popularity-aware retention。
#
# 当前统一模拟器不模拟 DHT discovery delay 和 block-level Merkle
# verification，因此这是思想级复现而不是官方 PeerSync 实现。
POLICY = PolicySpec(
    name="peersync_style",
    source_mode="peersync",
    ordering="fifo",
    coalesce=True,
    eviction="replica_popularity",
)
