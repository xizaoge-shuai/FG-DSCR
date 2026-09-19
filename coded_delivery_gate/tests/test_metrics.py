#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from gate.metrics import evaluate_gate_metrics


class MetricTests(unittest.TestCase):
    def test_two_node_reciprocal(self):
        sizes = {"A": 10, "B": 10}
        cache = {"n1": {"A"}, "n2": {"B"}}
        wants = {"n1": {"B"}, "n2": {"A"}}
        m = evaluate_gate_metrics(wants, cache, sizes)
        self.assertAlmostEqual(m["unicast_mb"], 20.0)
        self.assertAlmostEqual(m["mcast_mb"], 20.0)
        self.assertAlmostEqual(m["xor2_mb"], 10.0)
        self.assertAlmostEqual(m["xor2_gain_over_mcast"], 0.5)
        self.assertEqual(m["strict_xor2_matching_pairs"], 1)

    def test_common_demand_only_no_code_gain(self):
        sizes = {"A": 10}
        cache = {"n1": set(), "n2": set()}
        wants = {"n1": {"A"}, "n2": {"A"}}
        m = evaluate_gate_metrics(wants, cache, sizes)
        self.assertAlmostEqual(m["unicast_mb"], 20.0)
        self.assertAlmostEqual(m["mcast_mb"], 10.0)
        self.assertAlmostEqual(m["xor2_mb"], 10.0)
        self.assertAlmostEqual(m["xor2_gain_over_mcast"], 0.0)

    def test_partial_receiver_side_info_is_not_falsely_counted_as_mcast_saving(self):
        # n1/n2 都缺 A，但只有 n1 有 B；n3 缺 B 且有 A。
        # receiver-level 有局部 XOR 机会，但 A 仍要给 n2 发一次，
        # 因此相对 MCAST 不能直接声称节省。
        sizes = {"A": 10, "B": 10}
        cache = {"n1": {"B"}, "n2": set(), "n3": {"A"}}
        wants = {"n1": {"A"}, "n2": {"A"}, "n3": {"B"}}
        m = evaluate_gate_metrics(wants, cache, sizes)
        self.assertGreater(m["num_reciprocal_edges"], 0)
        self.assertEqual(m["strict_xor2_matching_pairs"], 0)
        self.assertAlmostEqual(m["xor2_mb"], m["mcast_mb"])

    def test_unequal_sizes_saves_min_size(self):
        sizes = {"A": 50, "B": 10}
        cache = {"n1": {"A"}, "n2": {"B"}}
        wants = {"n1": {"B"}, "n2": {"A"}}
        m = evaluate_gate_metrics(wants, cache, sizes)
        self.assertAlmostEqual(m["mcast_mb"], 60.0)
        self.assertAlmostEqual(m["strict_xor2_saving_mb"], 10.0)
        self.assertAlmostEqual(m["xor2_mb"], 50.0)


if __name__ == "__main__":
    unittest.main()
