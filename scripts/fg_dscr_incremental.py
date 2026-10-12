#!/usr/bin/env python3
"""FALCONS FGP: exact-form incremental *evaluation*, preserving legacy search.

This is a drop-in alternative to scripts/fg_dscr.py for the Computer Networks
major revision. It does NOT change the potential, constraints, neighborhood,
tie-breaking rule, beam ordering, PGDSF, or stopping criterion.

Because FGP normalizes fragmentation, affinity and load by GLOBAL statistics,
we do not incorrectly treat the total potential as a sum of independent
node-local deltas. Rather, we cache RAW node statistics, update only nodes
affected by the trial, then recompute the original global normalizers in O(M).
The original implementation remains untouched for matched verification.
"""
from __future__ import annotations

import importlib.util
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

_ORIGINAL = Path(__file__).with_name("fg_dscr.py")
_spec = importlib.util.spec_from_file_location("falcons_cn_original_fg", _ORIGINAL)
_base = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _base
_spec.loader.exec_module(_base)

Container = _base.Container
EdgeNode = _base.EdgeNode
load_case = _base.load_case


class FGDscrScheduler(_base.FGDscrScheduler):
    """Use locally updated raw statistics and exact original global scaling."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fg_ref_assignment = None
        self._fg_ref_states = None

    def set_data(self, containers, nodes):
        super().set_data(containers, nodes)
        self._fg_ref_assignment = None
        self._fg_ref_states = None

    def _new_empty_state(self):
        return {
            "count": 0,
            "runtime": 0.0,
            "used": defaultdict(float),
            "layers": Counter(),
            "raw_cong": 0.0,
            "raw_frag": 0.0,
            "raw_aff": 0.0,
            "cache_pressure": 0.0,
        }

    @staticmethod
    def _copy_state(st):
        return {
            **st,
            "used": defaultdict(float, st["used"]),
            "layers": st["layers"].copy(),
        }

    def _change_container(self, st, cid, sign):
        c = self.containers[cid]
        st["count"] += sign
        st["runtime"] += sign * c.run_time
        for q, v in c.resources.items():
            st["used"][q] += sign * v
        for layer in c.layers:
            st["layers"][layer] += sign
            # Zero counts are harmless: the original routines ignore k<=0.

    def _update_node_raw(self, eid, st):
        node = self.nodes[eid]
        layers = st["layers"]
        missing = self.distinct_missing_size(eid, layers)
        eps = 1e-9
        st["raw_cong"] = self.lambda_cong * (
            (missing ** 2) /
            (max(node.bandwidth_mb_s, eps) **
             max(getattr(self, "bw_gamma", 1.0), eps))
        )
        st["raw_aff"] = self.affinity_gain(eid, layers)

        if st["count"] == 0:
            st["raw_frag"] = 0.0
        else:
            residual_ratios = []
            pressure_ratios = []
            tiny_gap_pen = 0.0
            violation = None
            for q, capacity in node.resources.items():
                cap = max(float(capacity), 1e-8)
                used = float(st["used"].get(q, 0.0))
                if used > cap + 1e-9:
                    violation = 1e12 + (used - cap) * 1e9
                    break
                pressure = used / cap
                residual = max(0.0, cap - used) / cap
                pressure_ratios.append(pressure)
                residual_ratios.append(residual)
                if 0.0 < residual < self.tiny_gap_thresholds.get(q, 0.15):
                    tiny_gap_pen += 0.5
            if violation is not None:
                st["raw_frag"] = violation
            else:
                mr = sum(residual_ratios) / max(len(residual_ratios), 1)
                vr = sum((x - mr) ** 2 for x in residual_ratios) / max(len(residual_ratios), 1)
                mp = sum(pressure_ratios) / max(len(pressure_ratios), 1)
                vp = sum((x - mp) ** 2 for x in pressure_ratios) / max(len(pressure_ratios), 1)
                st["raw_frag"] = vr + 0.5 * vp + tiny_gap_pen

        if self.lambda_cache_core == 0.0:
            st["cache_pressure"] = 0.0
        else:
            cache_cap = float(getattr(node, "repo_capacity_mb", 0.0))
            if cache_cap > 0:
                core_mb = self.weighted_core_layer_size(
                    layers, getattr(self, "cache_core_ratio", 0.90)
                )
                st["cache_pressure"] = max(
                    0.0, core_mb / max(cache_cap, eps) - 1.0
                ) ** 2
            else:
                st["cache_pressure"] = 0.0

    def _build_states(self, assignment):
        states = {eid: self._new_empty_state() for eid in self.nodes}
        for cid, eid in assignment.items():
            self._change_container(states[eid], cid, +1)
        for eid, st in states.items():
            self._update_node_raw(eid, st)
        return states

    def _reference(self, assignment, states=None):
        self._fg_ref_assignment = dict(assignment)
        self._fg_ref_states = states if states is not None else self._build_states(assignment)

    def log_phase1_state(self, cycle, assignment, label):
        # Keep canonical component logs; refresh cached reference at *accepted*
        # placement states, not at individual move/swap candidates.
        super().log_phase1_state(cycle, assignment, label)
        self._reference(assignment)

    def _trial_states(self, assignment):
        if self._fg_ref_assignment is None:
            states = self._build_states(assignment)
            self._reference(assignment, states)
            return states

        reference = self._fg_ref_assignment
        states = self._fg_ref_states
        changes = []

        for cid, previous in reference.items():
            current = assignment.get(cid)
            if current != previous:
                changes.append((cid, previous, current))
        for cid, current in assignment.items():
            if cid not in reference:
                changes.append((cid, None, current))

        if not changes:
            return states

        # The greedy initializer grows one assignment at a time.
        # Rebase on a longer prefix for cheap future candidate comparisons.
        # Large changes are rare; rebuilding is safe and does not change scoring.
        if len(changes) > 8:
            new_states = self._build_states(assignment)
            self._reference(assignment, new_states)
            return new_states

        new_states = states.copy()
        touched = set()
        for cid, old, new in changes:
            if old is not None:
                if old not in touched:
                    new_states[old] = self._copy_state(new_states[old])
                    touched.add(old)
                self._change_container(new_states[old], cid, -1)
            if new is not None:
                if new not in touched:
                    new_states[new] = self._copy_state(new_states[new])
                    touched.add(new)
                self._change_container(new_states[new], cid, +1)

        for eid in touched:
            self._update_node_raw(eid, new_states[eid])

        if len(assignment) > len(reference):
            self._reference(assignment, new_states)

        return new_states

    def potential(self, assignment):
        states = self._trial_states(assignment)
        num_nodes = max(len(self.nodes), 1)
        total_num = max(len(assignment), 1)
        avg_per_node = total_num / num_nodes
        total_runtime = sum(self.containers[cid].run_time for cid in assignment)
        avg_run_per_node = total_runtime / num_nodes
        eps = 1e-9

        cong_scale = max(
            sum(abs(states[eid]["raw_cong"]) for eid in self.nodes) / num_nodes,
            1.0
        )
        frag_scale = max(max(
            (abs(states[eid]["raw_frag"]) for eid in self.nodes),
            default=0.0), eps)
        aff_scale = max(max(
            (abs(states[eid]["raw_aff"]) for eid in self.nodes),
            default=0.0), eps)

        raw_load = {}
        for eid, st in states.items():
            count = st["count"]
            soft_limit = self.task_load_factor * avg_per_node
            overload = max(0.0, count - soft_limit)
            overload_ratio = overload / max(avg_per_node, eps)
            run_ratio = st["runtime"] / max(avg_run_per_node, eps)
            raw_load[eid] = (overload_ratio ** self.task_load_power) * run_ratio

        load_scale = max(max((abs(v) for v in raw_load.values()), default=0.0), eps)

        total = 0.0
        for eid in self.nodes:
            st = states[eid]
            # Identical algebra to base.potential_components().
            total += (
                st["raw_cong"]
                + self.lambda_frag * cong_scale * (st["raw_frag"] / frag_scale)
                - self.lambda_aff * cong_scale * (st["raw_aff"] / aff_scale)
                + self.lambda_task_load * cong_scale *
                    (raw_load[eid] / load_scale if load_scale > eps else 0.0)
                + self.lambda_cache_core * cong_scale * st["cache_pressure"]
            )
        return total


def main():
    # Reuse all original CLI arguments, output format and simulator code.
    _base.FGDscrScheduler = FGDscrScheduler
    _base.main()


if __name__ == "__main__":
    main()
