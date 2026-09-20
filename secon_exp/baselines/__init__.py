from .registry_native import POLICY as REGISTRY
from .dragonfly_style import POLICY as DRAGONFLY_STYLE
from .peersync_style import POLICY as PEERSYNC_STYLE
from .metapipe_reactive_style import POLICY as METAPIPE_REACTIVE_STYLE
from .ilrsa_style import POLICY as ILRSA_STYLE


BASELINES = {
    p.name: p
    for p in (
        REGISTRY,
        DRAGONFLY_STYLE,
        PEERSYNC_STYLE,
        METAPIPE_REACTIVE_STYLE,
        ILRSA_STYLE,
    )
}
