from .base import PolicySpec

POLICY = PolicySpec(
    name="cloud_fifo",
    source_mode="cloud",
    ordering="fifo",
)
