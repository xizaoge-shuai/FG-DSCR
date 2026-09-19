from pathlib import Path
import re
import sys

SRC = Path("scripts/fg_dscr.py")
DST = Path("scripts/fg_dscr_online_norm.py")

s = SRC.read_text(encoding="utf-8")

# ============================================================
# 0) 插入 online norm helper
# ============================================================
helper = r'''
# =========================
# [ONLINE-NORM V2]
# Runtime component-wise normalization for decision making.
# Benefit terms: larger is better.
# Cost terms: smaller is better.
# =========================
def _fg_norm_benefit(x, lo, hi, eps=1e-12):
    if abs(hi - lo) < eps:
        return 0.5
    return (x - lo) / (hi - lo)

def _fg_norm_cost(x, lo, hi, eps=1e-12):
    if abs(hi - lo) < eps:
        return 0.5
    return (x - lo) / (hi - lo)

def _fg_online_norm_rows(rows, benefit_keys=(), cost_keys=(), eps=1e-12):
    if not rows:
        return rows

    for key in benefit_keys:
        vals = [float(r.get(key, 0.0)) for r in rows]
        lo, hi = min(vals), max(vals)
        for r in rows:
            x = float(r.get(key, 0.0))
            r[key + "_norm"] = 0.5 if abs(hi - lo) < eps else (x - lo) / (hi - lo)

    for key in cost_keys:
        vals = [float(r.get(key, 0.0)) for r in rows]
        lo, hi = min(vals), max(vals)
        for r in rows:
            x = float(r.get(key, 0.0))
            # saving score: larger is better
            r[key + "_save_norm"] = 0.5 if abs(hi - lo) < eps else (hi - x) / (hi - lo)

    return rows
'''

if "_fg_online_norm_rows" not in s:
    # 放在 import 后面
    m = re.search(r"((?:^import .*\n|^from .*\n)+)", s, flags=re.M)
    if m:
        s = s[:m.end()] + "\n" + helper + "\n" + s[m.end():]
    else:
        s = helper + "\n" + s

# ============================================================
# 1) __init__ 加 online_norm 参数和 self.online_norm
# ============================================================
if "online_norm: bool" not in s:
    s = s.replace(
        '        algo_name: str = "FG-DSCR",\n    ):',
        '        algo_name: str = "FG-DSCR",\n        online_norm: bool = True,\n    ):',
        1
    )

if "self.online_norm = online_norm" not in s:
    s = s.replace(
        "        self.algo_name = algo_name\n",
        "        self.algo_name = algo_name\n        self.online_norm = online_norm\n",
        1
    )

# ============================================================
# 2) 替换 potential_components：一阶段在线归一化
# ============================================================
new_potential_components = r'''    def potential_components(
        self,
        assignment: Dict[str, str],
    ) -> Tuple[float, Dict[str, Dict]]:
        """
        Phase-1 online-normalized potential.

        For the current assignment, compute raw node-level components first:
        - delay_raw: missing-layer delay / bandwidth cost
        - frag_raw: resource fragmentation cost
        - aff_raw: cache/layer affinity reward
        - load_raw: task load pressure cost

        Then normalize each component within the current node set.
        Cost terms use normal min-max cost values; affinity is a benefit term
        and is subtracted in the potential.
        """
        layer_cnts = self.node_layer_counts(assignment)
        node_counts = Counter(assignment.values())

        total_num = len(self.containers)
        num_nodes = max(len(self.nodes), 1)
        avg_per_node = total_num / num_nodes

        total_run_time = sum(c.run_time for c in self.containers.values())
        avg_run_per_node = total_run_time / num_nodes

        rows = []

        for eid, node in self.nodes.items():
            m_j = node_counts.get(eid, 0)
            D_j = self.distinct_missing_size(eid, layer_cnts[eid])
            Frag_j = self.fragmentation_penalty(eid, assignment)
            Aff_j = self.affinity_gain(eid, layer_cnts[eid])

            delay_raw = (D_j ** 2) / max(node.bandwidth_mb_s, 1e-8)

            load_ratio = m_j / max(avg_per_node, 1e-8)
            soft_limit = self.task_load_factor * avg_per_node
            overload = max(0.0, m_j - soft_limit)
            overload_ratio = overload / max(avg_per_node, 1e-8)

            node_run_sum = sum(
                self.containers[cid].run_time
                for cid, ne in assignment.items()
                if ne == eid
            )
            run_ratio = node_run_sum / max(avg_run_per_node, 1e-8)

            load_raw = (overload_ratio ** self.task_load_power) * run_ratio

            rows.append({
                "eid": eid,
                "m_j": m_j,
                "D_j": D_j,
                "Frag_j": Frag_j,
                "Aff_j": Aff_j,
                "delay_raw": delay_raw,
                "frag_raw": Frag_j,
                "aff_raw": Aff_j,
                "load_raw": load_raw,
                "load_ratio": load_ratio,
                "soft_limit": soft_limit,
                "overload": overload,
                "overload_ratio": overload_ratio,
                "node_run_sum": node_run_sum,
                "run_ratio": run_ratio,
            })

        # 当前 assignment 下，在节点集合内逐项归一化
        _fg_online_norm_rows(
            rows,
            benefit_keys=("aff_raw",),
            cost_keys=("delay_raw", "frag_raw", "load_raw"),
        )

        total = 0.0
        comps: Dict[str, Dict] = {}

        for r in rows:
            eid = r["eid"]

            # cost term: smaller raw is better, so potential uses 1 - saving
            delay_cost_norm = 1.0 - r["delay_raw_save_norm"]
            frag_cost_norm  = 1.0 - r["frag_raw_save_norm"]
            load_cost_norm  = 1.0 - r["load_raw_save_norm"]
            aff_reward_norm = r["aff_raw_norm"]

            delay_term = self.lambda_cong * delay_cost_norm
            frag_term = self.lambda_frag * frag_cost_norm
            aff_term = - self.lambda_aff * aff_reward_norm
            task_load_term = self.lambda_task_load * load_cost_norm

            node_val = delay_term + frag_term + aff_term + task_load_term
            total += node_val

            comps[eid] = {
                "m_j": r["m_j"],
                "D_j": r["D_j"],
                "Frag_j": r["Frag_j"],
                "Aff_j": r["Aff_j"],

                # raw components
                "delay_raw": r["delay_raw"],
                "frag_raw": r["frag_raw"],
                "aff_raw": r["aff_raw"],
                "load_raw": r["load_raw"],

                # normalized components
                "delay_norm": delay_cost_norm,
                "frag_norm": frag_cost_norm,
                "aff_norm": aff_reward_norm,
                "load_norm": load_cost_norm,

                # weighted terms used by actual potential
                "cong_term": delay_term,
                "frag_term": frag_term,
                "aff_term": aff_term,
                "task_load_term": task_load_term,

                "load_ratio": r["load_ratio"],
                "soft_limit": r["soft_limit"],
                "overload": r["overload"],
                "overload_ratio": r["overload_ratio"],
                "node_run_sum": r["node_run_sum"],
                "run_ratio": r["run_ratio"],

                "balance_term": 0.0,
                "idle_term": 0.0,
                "node_potential": node_val,
            }

        return total, comps
'''

pat = r'    def potential_components\(\n[\s\S]*?\n    def log_phase1_state\('
m = re.search(pat, s)
if not m:
    print("[ERROR] Cannot locate potential_components block.", file=sys.stderr)
    sys.exit(2)

s = s[:m.start()] + new_potential_components + "\n\n    def log_phase1_state(" + s[m.end():]

# ============================================================
# 3) 修改 score_candidate：记录 raw components，并把 future 改成 capacity-aware
# ============================================================
old_gain_block = '''        gain = (
            self.alpha1_reuse * reuse_mb
            + self.alpha2_future * future_share
            - self.alpha3_pull * pull_cost
            - self.alpha4_evict * evict_loss
        )
        return gain, new_cache, evicted, new_clock
'''

new_gain_block = '''        # Capacity-aware future:
        # only the current container layers retained after cache-capacity enforcement
        # can contribute to future reuse.
        retained_current_layers = set(c.layers) & set(new_cache)
        future_share_cap = 0.0
        for l in retained_current_layers:
            future_share_cap += self.layer_sizes_mb[l] * slot_future_cnt.get(l, 0)

        # Raw gain is kept for logging / non-online fallback.
        gain = (
            self.alpha1_reuse * reuse_mb
            + self.alpha2_future * future_share_cap
            - self.alpha3_pull * pull_cost
            - self.alpha4_evict * evict_loss
        )

        # Store raw components for the caller.
        self._last_score_candidate_components = {
            "cid": cid,
            "reuse": float(reuse_mb),
            "future": float(future_share_cap),
            "future_raw_non_capacity_aware": float(future_share),
            "pull": float(pull_cost),
            "evict": float(evict_loss),
            "gain_raw": float(gain),
            "num_evicted_layers": len(evicted),
        }

        return gain, new_cache, evicted, new_clock
'''

if old_gain_block not in s:
    print("[ERROR] Cannot locate score_candidate gain block.", file=sys.stderr)
    sys.exit(3)

s = s.replace(old_gain_block, new_gain_block, 1)

# ============================================================
# 4) 替换 order_node_with_beam 内部候选选择：二阶段在线归一化
# ============================================================
old_beam_block = '''            for st in beam:
                if not st.remaining:
                    new_beam.append(st)
                    continue

                # 可以全枚举；如果太大可以加筛选
                for cid in sorted(st.remaining):
                    gain, new_cache, _, new_clock = self.score_candidate(
                        cid=cid,
                        cache=st.cache,
                        remaining=st.remaining,
                        hist_freq=st.hist_freq,
                        clock=st.clock,
                        node=node,
                    )

                    new_hist = dict(st.hist_freq)
                    for l in self.containers[cid].layers:
                        new_hist[l] = new_hist.get(l, 0) + 1

                    nxt = BeamState(
                        seq=st.seq + [cid],
                        remaining=set(st.remaining - {cid}),
                        cache=new_cache,
                        hist_freq=new_hist,
                        clock=new_clock,
                        score=st.score + gain,
                    )
                    new_beam.append(nxt)

            # 保留 top-B
            new_beam.sort(key=lambda x: x.score, reverse=True)
'''

new_beam_block = '''            for st in beam:
                if not st.remaining:
                    new_beam.append(st)
                    continue

                candidate_rows = []

                # 可以全枚举；如果太大可以加筛选
                for cid in sorted(st.remaining):
                    gain_raw, new_cache, _, new_clock = self.score_candidate(
                        cid=cid,
                        cache=st.cache,
                        remaining=st.remaining,
                        hist_freq=st.hist_freq,
                        clock=st.clock,
                        node=node,
                    )

                    comps = dict(getattr(self, "_last_score_candidate_components", {}))

                    new_hist = dict(st.hist_freq)
                    for l in self.containers[cid].layers:
                        new_hist[l] = new_hist.get(l, 0) + 1

                    candidate_rows.append({
                        "cid": cid,
                        "gain_raw": float(gain_raw),
                        "reuse": float(comps.get("reuse", 0.0)),
                        "future": float(comps.get("future", 0.0)),
                        "pull": float(comps.get("pull", 0.0)),
                        "evict": float(comps.get("evict", 0.0)),
                        "new_cache": new_cache,
                        "new_clock": new_clock,
                        "new_hist": new_hist,
                    })

                if getattr(self, "online_norm", True):
                    _fg_online_norm_rows(
                        candidate_rows,
                        benefit_keys=("reuse", "future"),
                        cost_keys=("pull", "evict"),
                    )

                    for row in candidate_rows:
                        # Equal-weight normalized dynamic gain.
                        row["gain_used"] = (
                            row.get("reuse_norm", 0.5)
                            + row.get("future_norm", 0.5)
                            + row.get("pull_save_norm", 0.5)
                            + row.get("evict_save_norm", 0.5)
                        ) / 4.0
                else:
                    for row in candidate_rows:
                        row["gain_used"] = row["gain_raw"]

                for row in candidate_rows:
                    cid = row["cid"]
                    nxt = BeamState(
                        seq=st.seq + [cid],
                        remaining=set(st.remaining - {cid}),
                        cache=row["new_cache"],
                        hist_freq=row["new_hist"],
                        clock=row["new_clock"],
                        score=st.score + row["gain_used"],
                    )
                    new_beam.append(nxt)

            # 保留 top-B
            new_beam.sort(key=lambda x: x.score, reverse=True)
'''

if old_beam_block not in s:
    print("[ERROR] Cannot locate order_node_with_beam candidate loop.", file=sys.stderr)
    sys.exit(4)

s = s.replace(old_beam_block, new_beam_block, 1)

# ============================================================
# 5) CLI 添加 --online-norm，并传入 scheduler
# ============================================================
if '--online-norm' not in s:
    s = s.replace(
        '    parser.add_argument("--algo-name", type=str, default="FG-DSCR")\n',
        '    parser.add_argument("--algo-name", type=str, default="FG-DSCR")\n'
        '    parser.add_argument("--online-norm", action="store_true", help="Enable runtime online component-wise normalization.")\n',
        1
    )

if "online_norm=args.online_norm" not in s:
    s = s.replace(
        '        task_load_factor=args.task_load_factor,\n    )',
        '        task_load_factor=args.task_load_factor,\n'
        '        online_norm=args.online_norm,\n'
        '    )',
        1
    )

DST.write_text(s, encoding="utf-8")
print("[OK] wrote", DST)
print("[OK] Online-Norm V2 patches applied:")
print("  - phase1 potential_components replaced")
print("  - phase2 score_candidate uses capacity-aware future")
print("  - phase2 beam ordering uses online-normalized gain")
print("  - CLI --online-norm added")
