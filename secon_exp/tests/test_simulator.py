import unittest

from secon_exp.policies import (
    CLOUD_FIFO,
    P2P_SJF,
    C_LRU,
)
from secon_exp.simulator import (
    fair_rates,
    simulate,
)


def fixture(two_nodes=True):
    nodes = (
        ["n0", "n1"]
        if two_nodes
        else ["n0"]
    )

    return {
        "layer_sizes_mb": {
            "a": 100,
        },
        "containers": [
            {
                "cid": "x",
                "layers": ["a"],
            },
            {
                "cid": "y",
                "layers": ["a"],
            },
        ],
        "nodes": [
            {
                "eid": n,
                "bandwidth_mb_s": 100,
                "repo_capacity_mb": 1024,
                "initial_cache": [],
            }
            for n in nodes
        ],
    }


class SimulatorTests(unittest.TestCase):
    def test_fair_rates(self):
        self.assertEqual(
            fair_rates(
                [
                    ("w", "a"),
                    ("w", "b"),
                ],
                {
                    "w": 100,
                    "a": 20,
                    "b": 100,
                },
            ),
            [20, 80],
        )

    def test_cloud_duplicate_transfer(self):
        c = fixture()
        p = {
            "n0": ["x"],
            "n1": ["y"],
        }

        r = simulate(
            c,
            p,
            CLOUD_FIFO,
            wan=10,
            upload=100,
            lan=1000,
            cache_scale=None,
        )

        self.assertEqual(
            r["registry_mb"],
            200,
        )
        self.assertEqual(
            r["makespan_s"],
            20,
        )

    def test_p2p_coalescing(self):
        c = fixture()
        p = {
            "n0": ["x"],
            "n1": ["y"],
        }

        r = simulate(
            c,
            p,
            C_LRU,
            wan=10,
            upload=100,
            lan=1000,
            cache_scale=None,
        )

        self.assertEqual(
            r["registry_mb"],
            100,
        )
        self.assertEqual(
            r["peer_mb"],
            100,
        )
        self.assertEqual(
            sorted(r["ready_s"].values()),
            [10, 11],
        )

    def test_warm_peer(self):
        c = fixture()
        c["nodes"][0][
            "initial_cache"
        ] = ["a"]

        p = {
            "n0": ["x"],
            "n1": ["y"],
        }

        r = simulate(
            c,
            p,
            P2P_SJF,
            wan=1,
            upload=10,
            lan=1000,
            cache_scale=None,
        )

        self.assertEqual(
            r["ready_s"],
            {
                "x": 0,
                "y": 10,
            },
        )


if __name__ == "__main__":
    unittest.main()
