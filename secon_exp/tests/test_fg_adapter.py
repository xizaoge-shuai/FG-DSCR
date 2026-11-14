import unittest

from secon_exp.adapters.fg_dscr_adapter import (
    FGDSCRAdapter,
)


class FGAdapterTests(unittest.TestCase):
    def test_import(self):
        a = FGDSCRAdapter(
            "scripts/fg_dscr.py"
        )

        self.assertTrue(
            hasattr(
                a.mod,
                "FGDscrScheduler",
            )
        )

        self.assertTrue(
            hasattr(
                a.mod,
                "QueueMetrics",
            )
        )

    def test_window_split(self):
        case = {
            "layer_sizes_mb": {},
            "nodes": [],
            "containers": [
                {"cid": str(i)}
                for i in range(10)
            ],
        }

        warm, target = (
            FGDSCRAdapter.split_windows(
                case,
                warmup_fraction=0.5,
                seed=7,
            )
        )

        w = {
            x["cid"]
            for x in warm["containers"]
        }

        t = {
            x["cid"]
            for x in target[
                "containers"
            ]
        }

        self.assertFalse(w & t)
        self.assertEqual(
            len(w | t),
            10,
        )


if __name__ == "__main__":
    unittest.main()
