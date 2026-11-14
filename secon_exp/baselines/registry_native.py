from secon_exp.policies.base import PolicySpec

# 原生中心化 Registry：
# 不使用边缘 P2P，仅按照请求到达顺序拉取。
POLICY = PolicySpec(
    name="registry",
    source_mode="cloud",
    ordering="fifo",
    coalesce=False,
    eviction="lru",
)
