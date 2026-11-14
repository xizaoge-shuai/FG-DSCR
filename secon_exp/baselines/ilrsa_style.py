from secon_exp.policies.base import PolicySpec

# ILR-SA-style idea-level reproduction:
#
# - 利用 image-layer reuse 决定 layer transmission sequence；
# - 优先具有更高局部未来复用价值的 layer；
# - replacement 继续考虑未来 reuse；
# - 不额外加入 CIDER 的网络源联合优化。
POLICY = PolicySpec(
    name="ilrsa_style",
    source_mode="cloud",
    ordering="reuse",
    coalesce=False,
    eviction="future_reuse",
)
