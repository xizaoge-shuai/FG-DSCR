from secon_exp.policies.base import PolicySpec

# MetaPipe-Reactive-style:
#
# 为保证与 CIDER 使用相同的初始缓存状态，
# 不使用 MetaPipe 的 proactive prefetch 部分；
# 只复刻请求到达后的 reinforcement layer re-scheduling 思想。
#
# local ordering 表示根据当前 container readiness 动态调整
# layer transmission order。
POLICY = PolicySpec(
    name="metapipe_reactive_style",
    source_mode="p2p",
    ordering="local",
    coalesce=True,
    eviction="lru",
)
