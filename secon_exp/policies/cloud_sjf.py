from .base import PolicySpec

POLICY = PolicySpec(
    name="cloud_sjf",
    source_mode="cloud",
    ordering="sjf",
)
