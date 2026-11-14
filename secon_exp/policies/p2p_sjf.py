from .base import PolicySpec

POLICY = PolicySpec(
    name="p2p_sjf",
    source_mode="p2p",
    ordering="sjf",
)
