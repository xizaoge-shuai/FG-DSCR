#!/usr/bin/env python3
"""COMNET-D-26-04832 / Reviewer 2.5: controlled cache-policy comparison.

At each workload:
 - Run exactly ONE FALCONS placement and DCO ordering with PGDSF.
 - Freeze its placement AND startup order for all cache policies.
 - Re-evaluate the same request sequence under PGDSF, genuine LRU, genuine
   in-cache LFU and GreedyDual-Size-Frequency (GDSF).
 - Score all-request mean, P95, CA, fragmentation, and manuscript J using
   tools/comnet_revision/run_formal_parameter_audit.py definitions.
 - Report layer download volumes, cache reuse and eviction events.

No solver Obj/ACT from the legacy code is used as manuscript J. These
comparisons isolate replacement; they DO NOT test joint reoptimization of
startup ordering for each policy. LRU, LFU and GDSF do not use PGDSF's
future hints/pinning. Over-capacity single layers are transient downloads.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from statistics import mean

from run_formal_parameter_audit import (
    PROFILES, modload, run_one, raw_article_metrics, REF_D, REF_CA, REF_FRAG,
)

POLICIES = ("pgdsf", "lru", "lfu", "gdsf")


class StandardLayerCache:
    """Resident byte-bounded *layer* cache with deterministic tie-breaking.

    LRU: evict oldest actual layer access.
    LFU: evict fewest accesses since current admission (LRU tie-break).
    GDSF: H_i=L + F_i*C_i/S_i; retrieval C_i=S_i/bandwidth, with L
          updated to the priority of the most recently evicted layer.
    """

    def __init__(self, capacity_mb, bandwidth_mb_s, sizes, initial_cache):
        self.cap = max(0., float(capacity_mb))
        self.bw = max(1e-9, float(bandwidth_mb_s))
        self.sizes = sizes
        self.cache = set(initial_cache)
        self.touch = {layer: 0 for layer in self.cache}
        self.freq = {layer: 1 for layer in self.cache}
        self.priority = {
            layer: 1. / self.bw for layer in self.cache
        }
        self.clock = 0.
        self.tick = 0
        self.eviction_events = 0
        self.evicted_mb = 0.
        # The official workloads use an empty initial cache; reject silently
        # invalid initial snapshots instead of hiding a different experiment.
        if self.size(self.cache) > self.cap + 1e-8:
            raise ValueError("Initial cache exceeds the capacity")

    def size(self, layers):
        return sum(float(self.sizes[x]) for x in layers)

    def _key(self, layer, policy):
        if policy == "lru":
            return (self.touch[layer], str(layer))
        if policy == "lfu":
            return (self.freq[layer], self.touch[layer], str(layer))
        if policy == "gdsf":
            return (self.priority[layer], self.touch[layer], str(layer))
        raise ValueError(policy)

    def access(self, layer, policy):
        self.tick += 1
        hit = layer in self.cache
        evictions = []
        if hit:
            self.freq[layer] += 1
            self.touch[layer] = self.tick
            if policy == "gdsf":
                cost = float(self.sizes[layer]) / self.bw
                self.priority[layer] = self.clock + self.freq[layer] * cost / float(self.sizes[layer])
            return True, evictions

        size = float(self.sizes[layer])
        if size > self.cap or self.cap <= 0:
            return False, evictions  # download for this request but do not retain

        while self.size(self.cache) + size > self.cap + 1e-8:
            victim = min(self.cache, key=lambda l: self._key(l, policy))
            if policy == "gdsf":
                self.clock = max(self.clock, self.priority[victim])
            self.cache.remove(victim)
            self.touch.pop(victim)
            self.freq.pop(victim)
            self.priority.pop(victim)
            self.eviction_events += 1
            self.evicted_mb += float(self.sizes[victim])
            evictions.append(victim)

        self.cache.add(layer)
        self.touch[layer] = self.tick
        self.freq[layer] = 1
        self.priority[layer] = self.clock + (size / self.bw) / size
        return False, evictions


def replay_standard(case, model, original, policy):
    """Replay only replacement. Assignment and startup sequence are immutable."""
    if policy not in ("lru", "lfu", "gdsf"):
        raise ValueError(policy)

    ordered = original["ordered_queues"]
    assignment = original["assignment"]
    node_by_id = {str(n["eid"]): n for n in case["nodes"]}
    size_by_layer = {str(k): float(v) for k, v in case["layer_sizes_mb"].items()}
    cid_by_id = {str(c["cid"]): c for c in case["containers"]}

    per_request = []
    steps = []
    total_down = 0.
    total_reused = 0.
    total_evict_mb = 0.
    total_evict_count = 0

    for eid, cids in ordered.items():
        node = node_by_id[str(eid)]
        state = StandardLayerCache(
            node["repo_capacity_mb"], node["bandwidth_mb_s"], size_by_layer,
            node.get("initial_cache", []),
        )
        elapsed = 0.
        for step, cid in enumerate(cids, start=1):
            c = cid_by_id[str(cid)]
            # Reuse/download are counted against the cache BEFORE this request.
            # Layer updates then occur in a documented deterministic order.
            layers = sorted({str(l) for l in c["layers"]})
            cached_before = set(state.cache)
            reuse = sum(size_by_layer[l] for l in layers if l in cached_before)
            down = sum(size_by_layer[l] for l in layers if l not in cached_before)
            events_before = state.eviction_events
            mb_before = state.evicted_mb

            for l in layers:
                state.access(l, policy)

            ev_count = state.eviction_events - events_before
            ev_mb = state.evicted_mb - mb_before
            total_evict_count += ev_count
            total_evict_mb += ev_mb
            total_down += down
            total_reused += reuse
            wait = elapsed
            pull = down / max(1e-9, float(node["bandwidth_mb_s"]))
            elapsed += pull + float(c["run_time"])
            per_request.append({
                "cid": str(cid), "node_id": str(eid),
                "local_step": step, "completion_time": elapsed,
                "wait_time": wait, "pull_time": pull,
                "run_time": float(c["run_time"]),
                "reused_mb": reuse, "downloaded_mb": down,
                "cache_size_after_mb": state.size(state.cache),
                "evicted_mb": ev_mb, "eviction_events": ev_count,
            })
            steps.append({
                "cid":str(cid), "node_id":str(eid), "local_step":step,
                "downloaded_mb":down, "reuse_mb":reuse,
                "evicted_mb":ev_mb, "eviction_events":ev_count,
                "cache_size_after_mb":state.size(state.cache),
            })
        if state.size(state.cache) > state.cap + 1e-8:
            raise AssertionError(f"{policy}: node {eid} cache exceeds capacity")

    original_summary = original["summary"]
    num_total = int(original_summary["num_containers"])
    num_failed = int(original_summary["num_failed"])
    result = {
        "assignment": assignment,
        "ordered_queues": ordered,
        "container_metrics": per_request,
        "node_step_logs": steps,
        "summary": {
            "num_containers": num_total,
            "num_failed": num_failed,
            "downloaded_mb": total_down,
            "reused_mb": total_reused,
            "reuse_rate": total_reused / max(total_reused + total_down,1e-9),
        }
    }
    return result, total_evict_count, total_evict_mb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile",choices=list(PROFILES), default="paper_declared")
    ap.add_argument("--algorithm",default="scripts/fg_dscr_incremental.py")
    ap.add_argument("--case-dir",default="cases/drtp_cache_only_sweep_88")
    ap.add_argument("--provision-sec",type=float,default=300.)
    ap.add_argument("--out-dir",default="results/comnet_revision/cache_policy_standard")
    args=ap.parse_args()

    mod=modload(args.algorithm)
    out=Path(args.out_dir)
    out.mkdir(parents=True,exist_ok=True)
    scales=list(range(200,1001,100))
    data=[];frozen_info=[]

    for n in scales:
        cp=Path(args.case_dir)/f"drtp_img88_cacheonly_1024mb_{n}.json"
        case=json.loads(cp.read_text(encoding="utf-8"))
        full_metrics,full_ms,full_settings,original=run_one(
            mod,cp,args.profile,{},args.provision_sec
        )
        fixed_assignment=full_metrics["assignment_signature"]
        fixed_order=full_metrics["ordering_signature"]
        frozen_info.append({
            "requests":n,"assignment_signature":fixed_assignment,
            "ordering_signature":fixed_order,
            "FGP_DCO_controller_and_evaluation_wall_ms":full_ms,
        })
        for policy in POLICIES:
            if policy=="pgdsf":
                result=original
                steps=result["node_step_logs"]
                evicted_bytes=sum(float(x.get("evicted_mb",0.)) for x in steps)
                eviction_events=sum(int(x.get("num_evicted_layers",0)) for x in steps)
                metrics=full_metrics
            else:
                result,eviction_events,evicted_bytes=replay_standard(
                    case,mod,original,policy
                )
                metrics=raw_article_metrics(case,result,args.provision_sec)
            if result["assignment"]!=original["assignment"]:
                raise AssertionError(f"{n} {policy}: changed placement")
            if result["ordered_queues"]!=original["ordered_queues"]:
                raise AssertionError(f"{n} {policy}: changed order")
            expected=full_metrics["J"]
            if policy=="pgdsf" and abs(metrics["J"]-expected)>1e-10:
                raise AssertionError("PGDSF reference inconsistent")
            summary=result["summary"]
            rows={
                "profile":args.profile,"requests":n,"cache_policy":policy,
                "J":metrics["J"],"D":metrics["Delay_raw"],
                "all_request_mean":metrics["all_request_mean"],
                "P95_all":metrics["P95_all"],
                "CA_rate":metrics["CA_rate"],"Frag":metrics["frag_score"],
                "DownloadedMB":metrics["downloaded_mb"],
                "ReuseRate":float(summary.get("reuse_rate",0.)),
                "EvictionEvents":eviction_events,
                "EvictedMB":evicted_bytes,
                "DeltaJ_vs_PGDSF":metrics["J"]-expected,
                "AssignmentSignature":fixed_assignment,
                "OrderingSignature":fixed_order,
            }
            data.append(rows)
            print(f"N={n:4d} policy={policy:<5} J={rows['J']:.6f} "
                  f"DeltaJ={rows['DeltaJ_vs_PGDSF']:+.6f} "
                  f"DL={rows['DownloadedMB']:.0f}MB "
                  f"evict={eviction_events}",flush=True)

    csv_file=out/"cache_policy_by_scale.csv"
    with csv_file.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(data[0]));w.writeheader();w.writerows(data)
    totals=[]
    for policy in POLICIES:
        subset=[x for x in data if x["cache_policy"]==policy]
        totals.append({
            "policy":policy,
            **{k:mean(x[k] for x in subset) for k in (
                "J","D","all_request_mean","P95_all","CA_rate","Frag",
                "DownloadedMB","ReuseRate","EvictionEvents","EvictedMB",
                "DeltaJ_vs_PGDSF"
            )},
            "n_request_scales":len(subset),
        })
    with (out/"cache_policy_summary.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(totals[0]));w.writeheader();w.writerows(totals)
    with (out/"frozen_placement_order.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(frozen_info[0]));w.writeheader();w.writerows(frozen_info)

    provenance={
        "manuscript":"COMNET-D-26-04832",
        "reviewer_comment":"R2.5",
        "experiment":"replacement-only, placement and order frozen across policies",
        "profile":args.profile,
        "profile_note":"paper_declared (1,.1,.2,.2) differs from archival Fig8 main-results profile",
        "paper_J":"(.5*all_mean+.5*P95)/440.7696 + CA/0.77924 + Frag/0.180069",
        "reference_values":{"D_ref":REF_D,"CA_ref":REF_CA,"Frag_ref":REF_FRAG},
        "algorithm":args.algorithm,
        "full_scheduler_parameters":full_settings,
        "case_dir":args.case_dir,
        "provision_seconds":args.provision_sec,
        "cache_policies":{
            "PGDSF":"original author's policy, including future pins, discrete eviction and bounded cache",
            "LRU":"actual last layer access, evict oldest, deterministic layer order",
            "LFU":"residency-scoped hit count, LRU tie-breaking",
            "GDSF":"H=L + F*C/S, C=S/B=layer transfer seconds, eviction priority aging L",
        },
        "limitations":[
            "Policies use identical placement and queue ordering optimized for PGDSF; this isolates replacement rather than jointly reoptimizing each method.",
            "Baseline layers are accessed in ascending ID order within each container; this is a defined deterministic cache-access convention.",
            "LRU, LFU and GDSF use no PGDSF lookahead protection.",
            "Original simulation models serial container completion, not actual K8s concurrency.",
            "Nine scales are not independent random trials.",
            "Do NOT compare historical Fig8 J=2.697 directly against paper_declared cache experiment J."
        ],
    }
    (out/"provenance.json").write_text(json.dumps(provenance,ensure_ascii=False,indent=2),encoding="utf-8")
    print("\n=== R2.5 controlled cache policy comparison ===")
    for x in totals:
        print(f"{x['policy']:<5} J={x['J']:.6f} "
              f"Delta_vs_PGDSF={x['DeltaJ_vs_PGDSF']:+.6f} "
              f"DL={x['DownloadedMB']:.1f}MB evict={x['EvictionEvents']:.1f}")
    print("Saved:",csv_file)

if __name__=="__main__":
    main()
