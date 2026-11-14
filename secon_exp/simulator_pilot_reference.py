"""Network-only pilot: batch arrival, fixed placement, no eviction, no unpack/runtime."""
import argparse
import csv
import hashlib
import importlib.util
import json
import math
import sys
from collections import Counter
from pathlib import Path

POLICIES = ("cloud_fifo", "cloud_sjf", "p2p_sjf", "p2p_coalesce", "p2p_priority")


def fair_rates(paths, caps):
    """Progressive filling: max-min fair rates under every resource capacity."""
    rates = [0.0] * len(paths)
    free = set(range(len(paths)))
    left = dict(caps)
    while free:
        counts = Counter(r for i in free for r in paths[i])
        step = min(left[r] / k for r, k in counts.items())
        for i in free:
            rates[i] += step
        for r, k in counts.items():
            left[r] = max(0.0, left[r] - step * k)
        saturated = {r for r in counts if left[r] <= 1e-8}
        blocked = {i for i in free if saturated.intersection(paths[i])}
        if not blocked:
            raise RuntimeError("Bandwidth allocation failed to advance")
        free -= blocked
    return rates


def simulate(case, placement, policy, wan, upload, lan):
    if policy not in POLICIES:
        raise ValueError("Unknown policy")
    sizes = {l: float(s) for l, s in case["layer_sizes_mb"].items()}
    nodes = {n["eid"]: n for n in case["nodes"]}
    tasks = {c["cid"]: set(c["layers"]) for c in case["containers"]}
    if len(tasks) != len(case["containers"]) or len(nodes) != len(case["nodes"]):
        raise ValueError("Duplicate request/node IDs")
    if any(not math.isfinite(s) or s < 0 for s in sizes.values()):
        raise ValueError("Invalid layer sizes")
    assigned = []
    for n, ids in placement.items():
        if n not in nodes or not isinstance(ids, list):
            raise ValueError("Expected assignment: node_id -> list of request IDs")
        assigned.extend(ids)
    if len(assigned) != len(set(assigned)) or not set(assigned) <= tasks.keys():
        raise ValueError("Duplicate or unknown requests in assignment")
    groups = {n: list(placement.get(n, [])) for n in sorted(nodes)}
    cache = {n: set(nodes[n].get("initial_cache", [])) for n in groups}
    need = {n: set().union(*(tasks[c] for c in groups[n])) for n in groups}
    if any(not (need[n] | cache[n]) <= sizes.keys() for n in groups):
        raise ValueError("Missing layer size")
    caps = {"wan": float(wan), "lan": float(lan)}
    for n in groups:
        caps["rx:" + n] = float(nodes[n]["bandwidth_mb_s"])
        caps["tx:" + n] = float(upload)
        cache[n].update(l for l in need[n] if sizes[l] == 0)
    if any(not math.isfinite(x) or x <= 0 for x in caps.values()):
        raise ValueError("All bandwidths must be finite and positive")
    order = {c: i for i, c in enumerate(tasks)}
    fifo = {n: {l: min(order[c] for c in groups[n] if l in tasks[c])
                for l in need[n]} for n in groups}
    ready, active, events = {}, [], []
    traffic = {"registry_mb": 0.0, "peer_mb": 0.0}
    t = 0.0
    while True:
        for n in groups:
            for c in groups[n]:
                if c not in ready and tasks[c] <= cache[n]:
                    ready[c] = t
        if len(ready) == len(assigned):
            break
        missing = {n: need[n] - cache[n] for n in groups}
        busy = {f["dst"] for f in active}
        for n in groups:
            if n in busy:
                continue
            local = Counter()
            if policy == "p2p_priority":
                for c in groups[n]:
                    remaining = tasks[c] - cache[n]
                    for l in remaining:
                        local[l] += 1.0 / len(remaining)

            def priority(l):
                if policy == "cloud_fifo":
                    return (fifo[n][l], l)
                if policy == "p2p_priority":
                    fanout = sum(l in missing[k] for k in groups if k != n)
                    return (-(local[l] + 0.25 * fanout) / sizes[l], l)
                return (sizes[l], l)

            usage = Counter(r for f in active for r in f["path"])
            for l in sorted(missing[n], key=priority):
                sources = [None]
                if policy.startswith("p2p"):
                    sources += [s for s in groups if s != n and l in cache[s]]
                options = []
                for s in sources:
                    path = ("wan" if s is None else "tx:" + s, "rx:" + n, "lan")
                    estimate = min(caps[r] / (usage[r] + 1) for r in path)
                    options.append((-estimate, s is None, s or "", path))
                _, cloud, source, path = min(options)
                coalesce = policy in ("p2p_coalesce", "p2p_priority")
                if coalesce and cloud and any(f["layer"] == l for f in active):
                    continue
                active.append(dict(src=None if cloud else source, dst=n, layer=l,
                                   path=path, remaining=sizes[l], start_s=t))
                break
        if not active:
            raise RuntimeError("Unfinished requests but no possible transfer")
        rates = fair_rates([f["path"] for f in active], caps)
        if min(rates) <= 0:
            raise RuntimeError("Non-positive rate")
        for resource, capacity in caps.items():
            used = sum(r for f, r in zip(active, rates) if resource in f["path"])
            if used > capacity + 1e-7 * max(1.0, capacity):
                raise RuntimeError("Link capacity violated")
        dt = min(f["remaining"] / r for f, r in zip(active, rates))
        t += dt
        pending = []
        for f, rate in zip(active, rates):
            f["remaining"] -= rate * dt
            if f["remaining"] > 1e-7:
                pending.append(f)
                continue
            cache[f["dst"]].add(f["layer"])
            amount = sizes[f["layer"]]
            traffic["registry_mb" if f["src"] is None else "peer_mb"] += amount
            events.append(dict(src=f["src"], dst=f["dst"], layer=f["layer"],
                               size_mb=amount, start_s=f["start_s"], end_s=t))
        active = pending
    values = sorted(ready.values())
    peak = {n: sum(sizes[l] for l in cache[n]) for n in groups}
    overflow = [n for n in groups if peak[n] > float(nodes[n]["repo_capacity_mb"]) + 1e-8]
    return dict(policy=policy, wan_mb_s=wan, upload_mb_s=upload, lan_mb_s=lan,
                accepted=len(assigned), excluded=len(tasks) - len(assigned),
                mean_ready_s=sum(values) / max(1, len(values)),
                p95_ready_s=values[max(0, math.ceil(.95 * len(values)) - 1)] if values else 0,
                makespan_s=max(values, default=0), **traffic,
                total_transfer_mb=sum(traffic.values()),
                cache_overflow_nodes=overflow, retained_mb=peak,
                ready_s=ready, events=events)


def self_test():
    def fixture(two_nodes=True):
        return dict(layer_sizes_mb={"a": 100},
                    containers=[dict(cid="x", layers=["a"]), dict(cid="y", layers=["a"])],
                    nodes=[dict(eid=n, bandwidth_mb_s=100, repo_capacity_mb=1024,
                                initial_cache=[]) for n in (["n0", "n1"] if two_nodes else ["n0"])])
    assert fair_rates([("w", "a"), ("w", "b")], {"w": 100, "a": 20, "b": 100}) == [20, 80]
    c = fixture()
    p = {"n0": ["x"], "n1": ["y"]}
    cloud = simulate(c, p, "cloud_fifo", 10, 100, 1000)
    peer = simulate(c, p, "p2p_coalesce", 10, 100, 1000)
    assert cloud["registry_mb"] == 200 and cloud["makespan_s"] == 20
    assert peer["registry_mb"] == 100 and peer["peer_mb"] == 100
    assert sorted(peer["ready_s"].values()) == [10, 11]
    local = simulate(fixture(False), {"n0": ["x", "y"]}, "cloud_fifo", 50, 100, 1000)
    assert local["registry_mb"] == 100 and local["makespan_s"] == 2
    c["nodes"][0]["initial_cache"] = ["a"]
    assert simulate(c, p, "p2p_sjf", 1, 10, 1000)["ready_s"] == {"x": 0, "y": 10}
    print("Self-tests passed: capacity sharing, deduplication, replica causality, warm cache")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", default="cases/drtp_large_v2/drtp_img88_cache_1024mb_200.json")
    ap.add_argument("--fg", default="scripts/fg_dscr.py")
    ap.add_argument("--placement", help="Optional existing FG JSON result")
    ap.add_argument("--out", default="results/secon_comm_pilot")
    ap.add_argument("--wan", type=float, nargs="+", default=[10, 30, 100])
    ap.add_argument("--upload", type=float, default=100)
    ap.add_argument("--lan", type=float, default=1000)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    if any(not math.isfinite(v) or v <= 0 for v in args.wan + [args.upload, args.lan]):
        ap.error("Bandwidths must be finite and positive")
    if not Path(args.case).is_file():
        ap.error("Case not found; pass --case /path/to/an/existing/case.json")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    case = json.loads(Path(args.case).read_text(encoding="utf-8"))
    fg_params = dict(beam_width=1, phase1_neighbor_mode="move", max_best_response_rounds=5,
                     move_topk_per_node=6, hard_resource_filter=True, lambda_fail=1000,
                     lambda_cong=1, lambda_frag=10, lambda_aff=0, k_pin=6,
                     cache_policy="pgdsf", order_policy="dynamic_state",
                     greedy_load_factor=.8, init_order="resource_asc")
    if args.placement:
        result = json.loads(Path(args.placement).read_text(encoding="utf-8"))
    else:
        spec = importlib.util.spec_from_file_location("_fg_comm_pilot", args.fg)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        containers, nodes, sizes = module.load_case(args.case)
        scheduler = module.FGDscrScheduler(layer_sizes_mb=sizes, **fg_params)
        scheduler.set_data(containers, nodes)
        result = scheduler.run()
    placement = result["assignment"]
    if not any(placement.values()):
        raise ValueError("No accepted requests; choose a feasible case")
    (out / "placement.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    inputs = [args.case, __file__, args.placement or args.fg]
    meta = dict(evaluation="simulation_only_pilot", arguments=vars(args),
                fg_params=None if args.placement else fg_params,
                assumptions=["all requests arrive at t=0", "fixed placement, accepted requests only",
                             "no eviction; ignore original cache limit", "one download per node",
                             "max-min sharing: registry WAN, peer uplink, receiver, shared LAN",
                             "full-duplex node ports; completed replicas only",
                             "no RTT, unpack, initialization, or run_time",
                             "legacy MB fields used consistently; original precision unchanged"],
                hashes={str(p): hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in inputs})
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    rows = []
    print("WAN   policy           accepted/excluded  mean_s   p95_s   registry_MB  peer_MB  overcap")
    for index, wan in enumerate(args.wan):
        for policy in POLICIES:
            r = simulate(case, placement, policy, wan, args.upload, args.lan)
            (out / f"{index}_{policy}.json").write_text(json.dumps(r, indent=2), encoding="utf-8")
            row = {k: v for k, v in r.items() if k not in ("events", "ready_s", "retained_mb", "cache_overflow_nodes")}
            row["overcap_nodes"] = len(r["cache_overflow_nodes"])
            rows.append(row)
            print(f"{wan:<5g} {policy:<16} {r['accepted']:>4}/{r['excluded']:<4}"
                  f" {r['mean_ready_s']:9.2f} {r['p95_ready_s']:8.2f}"
                  f" {r['registry_mb']:12.0f} {r['peer_mb']:8.0f} {row['overcap_nodes']:7}")
    with (out / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print("Saved:", out.resolve())
    print("PILOT ONLY: no-eviction network replay; overcap reports original cache-limit violations.")


if __name__ == "__main__":
    main()
