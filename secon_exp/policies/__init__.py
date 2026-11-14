from .cloud_fifo import POLICY as CLOUD_FIFO
from .cloud_sjf import POLICY as CLOUD_SJF
from .p2p_sjf import POLICY as P2P_SJF
from .p2p_coalesce import POLICY as C_LRU
from .local_priority import POLICY as L_LRU
from .relay_lease import POLICY as L_LEASE
from .debt_lease import POLICY as D_LEASE

POLICIES = {
    p.name: p
    for p in (
        CLOUD_FIFO,
        CLOUD_SJF,
        P2P_SJF,
        C_LRU,
        L_LRU,
        L_LEASE,
        D_LEASE,
    )
}
