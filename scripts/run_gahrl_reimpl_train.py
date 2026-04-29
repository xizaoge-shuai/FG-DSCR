import argparse
import json
import os
import random
from collections import Counter, deque

import numpy as np

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
except Exception as e:
    raise RuntimeError(
        "This script requires PyTorch. Please install torch first, or run the GAHRL-objective-greedy baseline instead."
    ) from e


def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def node_id(n):
    return n.get("eid") or n.get("id") or n.get("nid") or n.get("name") or n.get("node_id")


def layer_mb(layer_sizes, l):
    v = layer_sizes.get(l, 0)
    try:
        v = float(v)
    except Exception:
        v = 0.0
    if v <= 0:
        v = 1.0
    return v


def c_layers(c):
    return list(c.get("layers", []) or [])


def c_res(c, key):
    return float((c.get("resources", {}) or {}).get(key, 0.0))


def n_res(n, key):
    return float((n.get("resources", {}) or {}).get(key, 0.0))


def container_size(c, layer_sizes):
    return sum(layer_mb(layer_sizes, l) for l in c_layers(c))


def build_layer_pop(containers):
    pop = Counter()
    for c in containers:
        for l in set(c_layers(c)):
            pop[l] += 1
    return pop


def cache_size(cache, layer_sizes):
    return sum(layer_mb(layer_sizes, l) for l in cache)


def reused_missing_mb(c, cache, layer_sizes):
    reused = 0.0
    missing = 0.0
    for l in c_layers(c):
        if l in cache:
            reused += layer_mb(layer_sizes, l)
        else:
            missing += layer_mb(layer_sizes, l)
    return reused, missing


def evict_to_fit(cache, cap, layer_sizes, layer_pop, protected=None):
    protected = set(protected or [])
    if cache_size(cache, layer_sizes) <= cap:
        return cache

    def key(l):
        return (layer_pop.get(l, 0), -layer_mb(layer_sizes, l))

    removable = [l for l in cache if l not in protected]
    removable.sort(key=key)

    for l in removable:
        if cache_size(cache, layer_sizes) <= cap:
            break
        cache.remove(l)

    if cache_size(cache, layer_sizes) > cap:
        rest = list(cache)
        rest.sort(key=key)
        for l in rest:
            if cache_size(cache, layer_sizes) <= cap:
                break
            cache.remove(l)

    return cache


def add_to_cache(cache, layers, cap, layer_sizes, layer_pop):
    layers = list(layers)
    for l in layers:
        cache.add(l)
    return evict_to_fit(cache, cap, layer_sizes, layer_pop, protected=layers)


class ReplayBuffer:
    def __init__(self, cap):
        self.buf = deque(maxlen=cap)

    def push(self, item):
        self.buf.append(item)

    def sample(self, batch_size):
        batch = random.sample(self.buf, batch_size)
        return list(zip(*batch))

    def __len__(self):
        return len(self.buf)


class GAHRLEnv:
    def __init__(self, case, args):
        self.case = case
        self.args = args
        self.layer_sizes = case["layer_sizes_mb"]
        self.containers = case["containers"]
        self.nodes = case["nodes"]
        self.M = len(self.nodes)
        self.layer_pop = build_layer_pop(self.containers)

        self.max_cpu = max(n_res(n, "cpu") for n in self.nodes) or 1.0
        self.max_mem = max(n_res(n, "mem") for n in self.nodes) or 1.0
        self.max_disk = max(n_res(n, "disk") for n in self.nodes) or 1.0
        self.max_bw = max(float(n.get("bandwidth_mb_s", 1.0)) for n in self.nodes) or 1.0

        self.avg_csize = sum(container_size(c, self.layer_sizes) for c in self.containers) / max(len(self.containers), 1)
        self.avg_runtime = sum(float(c.get("run_time", 0.0)) for c in self.containers) / max(len(self.containers), 1)
        self.norm_storage = max(self.avg_csize, 1.0)
        self.norm_time = max(self.avg_runtime + self.avg_csize / self.max_bw, 1.0)

        self.reset()

    def reset(self):
        self.idx = 0
        self.caches = {}
        self.used = {}
        self.queue_finish = {}
        self.assignment = {}
        self.allocations = {}

        for n in self.nodes:
            nid = node_id(n)
            self.caches[nid] = set(n.get("initial_cache", []) or [])
            self.used[nid] = {"cpu": 0.0, "mem": 0.0, "disk": 0.0}
            self.queue_finish[nid] = 0.0
            self.assignment[nid] = []
            self.allocations[nid] = []

        return self.state()

    def current_container(self):
        return self.containers[self.idx]

    def feasible_mask(self, c):
        mask = []
        for n in self.nodes:
            ok = True
            for k in ["cpu", "mem", "disk"]:
                if c_res(c, k) > n_res(n, k):
                    ok = False
            mask.append(ok)
        return np.asarray(mask, dtype=np.bool_)

    def resource_terms(self, c, n, nid):
        ratios = []
        for k in ["cpu", "mem", "disk"]:
            cap = max(n_res(n, k), 1e-9)
            ratios.append((self.used[nid][k] + c_res(c, k)) / cap)
        avg = sum(ratios) / len(ratios)
        max_pressure = max(ratios)
        imbalance = sum(abs(x - avg) for x in ratios) / len(ratios)
        return max_pressure, imbalance

    def state(self):
        if self.idx >= len(self.containers):
            c = self.containers[-1]
        else:
            c = self.current_container()

        feats = []
        hit_list = []
        miss_list = []

        for n in self.nodes:
            nid = node_id(n)
            reused, missing = reused_missing_mb(c, self.caches[nid], self.layer_sizes)
            total = max(reused + missing, 1e-9)
            hit = reused / total
            miss_norm = missing / self.norm_storage
            hit_list.append(hit)
            miss_list.append(miss_norm)

            max_pressure, imbalance = self.resource_terms(c, n, nid)
            cap = float(n.get("repo_capacity_mb", 1024))
            new_layers = [l for l in c_layers(c) if l not in self.caches[nid]]
            new_cache_mb = cache_size(self.caches[nid], self.layer_sizes) + sum(layer_mb(self.layer_sizes, l) for l in new_layers)
            cache_pressure = max(0.0, new_cache_mb - cap) / max(cap, 1e-9)

            cpu_cap = n_res(n, "cpu")
            mem_cap = n_res(n, "mem")
            disk_cap = n_res(n, "disk")
            bw = float(n.get("bandwidth_mb_s", 1.0))

            feat = [
                cpu_cap / self.max_cpu,
                mem_cap / self.max_mem,
                disk_cap / self.max_disk,
                bw / self.max_bw,
                self.used[nid]["cpu"] / max(cpu_cap, 1e-9),
                self.used[nid]["mem"] / max(mem_cap, 1e-9),
                self.used[nid]["disk"] / max(disk_cap, 1e-9),
                self.queue_finish[nid] / self.norm_time,
                hit,
                miss_norm,
                cache_pressure,
                max_pressure,
                imbalance,
                c_res(c, "cpu") / max(cpu_cap, 1e-9),
                c_res(c, "mem") / max(mem_cap, 1e-9),
                c_res(c, "disk") / max(disk_cap, 1e-9),
            ]
            feats.append(feat)

        fm = hit_list + miss_list + [
            container_size(c, self.layer_sizes) / self.norm_storage,
            c_res(c, "cpu") / self.max_cpu,
            c_res(c, "mem") / self.max_mem,
            float(c.get("run_time", 0.0)) / max(self.avg_runtime, 1e-9),
        ]

        mask = self.feasible_mask(c)
        return (
            np.asarray(feats, dtype=np.float32),
            np.asarray(fm, dtype=np.float32),
            mask,
        )

    def step(self, action, f_value):
        c = self.current_container()
        n = self.nodes[action]
        nid = node_id(n)

        mask = self.feasible_mask(c)
        if not mask[action]:
            reward = -100.0
            self.idx += 1
            done = self.idx >= len(self.containers)
            return self.state(), reward, done

        bw = float(n.get("bandwidth_mb_s", 1.0))
        cap = float(n.get("repo_capacity_mb", 1024))

        reused, missing = reused_missing_mb(c, self.caches[nid], self.layer_sizes)
        startup_time = missing / max(bw, 1e-9)

        # GAHRL 原文有连续资源分配 f_q。这里让 f_value 只影响训练 reward，
        # 最终汇总仍使用标准 simulator 的 run_time，避免和 FG-DSCR 指标不一致。
        f_value = float(np.clip(f_value, 0.05, 1.0))
        computation_proxy = float(c.get("run_time", 0.0)) / (0.5 + f_value)

        service_latency_proxy = self.queue_finish[nid] + startup_time + computation_proxy
        storage_cost = missing

        max_pressure, imbalance = self.resource_terms(c, n, nid)

        total = max(reused + missing, 1e-9)
        hit = reused / total

        latency_term = service_latency_proxy / self.norm_time
        storage_term = storage_cost / self.norm_storage

        reward = -(
            self.args.lambda_latency * latency_term
            + (1.0 - self.args.lambda_latency) * storage_term
            + self.args.w_resource * max_pressure
            + self.args.w_imbalance * imbalance
            - self.args.w_layer_hit * hit
        )

        self.assignment[nid].append(c["cid"])
        self.allocations[nid].append({"cid": c["cid"], "f": f_value})

        # 真实排队状态用原始 run_time，保证最后 ACT/AMS 和其他算法同口径
        self.queue_finish[nid] += startup_time + float(c.get("run_time", 0.0))

        for k in ["cpu", "mem", "disk"]:
            self.used[nid][k] += c_res(c, k)

        miss_layers = [l for l in c_layers(c) if l not in self.caches[nid]]
        self.caches[nid] = add_to_cache(self.caches[nid], miss_layers, cap, self.layer_sizes, self.layer_pop)

        self.idx += 1
        done = self.idx >= len(self.containers)
        next_state = self.state()
        return next_state, reward, done


class GAHRLNet(nn.Module):
    def __init__(self, node_dim, fm_dim, hidden, num_nodes):
        super().__init__()
        self.num_nodes = num_nodes

        self.gcn1 = nn.Linear(node_dim, hidden)
        self.gcn2 = nn.Linear(hidden, hidden)

        self.fm_linear = nn.Linear(fm_dim, hidden)
        self.fm_v = nn.Parameter(torch.randn(fm_dim, hidden) * 0.01)

        self.actor = nn.Sequential(
            nn.Linear(hidden * 2, hidden),
            nn.ReLU(),
            nn.Linear(hidden, num_nodes),
            nn.Sigmoid(),
        )

        self.value = nn.Sequential(
            nn.Linear(hidden * 2, hidden),
            nn.ReLU(),
            nn.Linear(hidden, 1),
        )

        self.adv = nn.Sequential(
            nn.Linear(hidden * 3 + 1, hidden),
            nn.ReLU(),
            nn.Linear(hidden, 1),
        )

    def encode_fm(self, fm_x):
        linear = self.fm_linear(fm_x)
        xv = torch.matmul(fm_x, self.fm_v)
        x2v2 = torch.matmul(fm_x * fm_x, self.fm_v * self.fm_v)
        inter = 0.5 * (xv * xv - x2v2)
        return torch.relu(linear + inter)

    def encode_gcn(self, node_x):
        # fully-connected graph with self-loop, normalized by number of nodes
        B, M, D = node_x.shape
        h = torch.relu(self.gcn1(node_x))
        h = h.mean(dim=1, keepdim=True).repeat(1, M, 1) + h
        h = torch.relu(self.gcn2(h))
        return h

    def actor_f(self, node_x, fm_x):
        node_h = self.encode_gcn(node_x)
        graph_h = node_h.mean(dim=1)
        fm_h = self.encode_fm(fm_x)
        global_h = torch.cat([graph_h, fm_h], dim=-1)
        f = self.actor(global_h)
        return 0.05 + 0.95 * f

    def q_values(self, node_x, fm_x, f_vec):
        node_h = self.encode_gcn(node_x)
        graph_h = node_h.mean(dim=1)
        fm_h = self.encode_fm(fm_x)
        global_h = torch.cat([graph_h, fm_h], dim=-1)

        V = self.value(global_h)

        B, M, H = node_h.shape
        global_rep = global_h.unsqueeze(1).repeat(1, M, 1)
        f_rep = f_vec.unsqueeze(-1)
        adv_in = torch.cat([node_h, global_rep, f_rep], dim=-1)

        A = self.adv(adv_in).squeeze(-1)
        Q = V + A - A.mean(dim=1, keepdim=True)
        return Q


def simulate_standard(case, assignment):
    layer_sizes = case["layer_sizes_mb"]
    containers = case["containers"]
    nodes = case["nodes"]
    c_by_id = {c["cid"]: c for c in containers}
    n_by_id = {node_id(n): n for n in nodes}
    layer_pop = build_layer_pop(containers)

    ordered_queues = {nid: list(cids) for nid, cids in assignment.items()}
    node_details = {}
    container_metrics = {}

    total_downloaded = 0.0
    total_reused = 0.0
    all_finish = []

    for nid, cids in ordered_queues.items():
        n = n_by_id[nid]
        cache = set(n.get("initial_cache", []) or [])
        cap = float(n.get("repo_capacity_mb", 1024))
        bw = float(n.get("bandwidth_mb_s", 1.0))

        t = 0.0
        node_downloaded = 0.0
        node_reused = 0.0

        for cid in cids:
            c = c_by_id[cid]
            reused, missing = reused_missing_mb(c, cache, layer_sizes)

            download_time = missing / max(bw, 1e-9)
            run_time = float(c.get("run_time", 0.0))

            start = t
            finish = t + download_time + run_time
            t = finish

            total_downloaded += missing
            total_reused += reused
            node_downloaded += missing
            node_reused += reused
            all_finish.append(finish)

            container_metrics[cid] = {
                "node": nid,
                "start_time": start,
                "finish_time": finish,
                "downloaded_mb": missing,
                "reused_mb": reused,
                "download_time": download_time,
                "run_time": run_time,
            }

            miss_layers = [l for l in c_layers(c) if l not in cache]
            cache = add_to_cache(cache, miss_layers, cap, layer_sizes, layer_pop)

        node_details[nid] = {
            "num_containers": len(cids),
            "finish_time": t,
            "downloaded_mb": node_downloaded,
            "reused_mb": node_reused,
            "final_cache_mb": cache_size(cache, layer_sizes),
            "final_cache_layers": len(cache),
        }

    ACT = sum(all_finish) / max(len(all_finish), 1)
    AMS = max(all_finish) if all_finish else 0.0
    objective = 0.5 * ACT + 0.5 * AMS
    reuse_rate = total_reused / max(total_reused + total_downloaded, 1e-9)

    return {
        "ordered_queues": ordered_queues,
        "summary": {
            "algo": "GAHRL-reimpl",
            "num_containers": len(containers),
            "num_nodes": len(nodes),
            "ACT": ACT,
            "AMS": AMS,
            "objective": objective,
            "downloaded_mb": int(round(total_downloaded)),
            "reused_mb": int(round(total_reused)),
            "reuse_rate": reuse_rate,
        },
        "node_details": node_details,
        "container_metrics": container_metrics,
    }


def train_and_eval(case, args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    env = GAHRLEnv(case, args)
    node_x, fm_x, mask = env.state()

    num_nodes = env.M
    node_dim = node_x.shape[1]
    fm_dim = fm_x.shape[0]

    device = torch.device("cuda" if torch.cuda.is_available() and not args.cpu else "cpu")

    net = GAHRLNet(node_dim, fm_dim, args.hidden, num_nodes).to(device)
    target = GAHRLNet(node_dim, fm_dim, args.hidden, num_nodes).to(device)
    target.load_state_dict(net.state_dict())

    opt_q = torch.optim.Adam(net.parameters(), lr=args.lr)
    opt_actor = torch.optim.Adam(net.parameters(), lr=args.lr * 0.5)

    replay = ReplayBuffer(args.replay_size)

    eps = args.eps_start

    def to_t(x):
        return torch.tensor(x, dtype=torch.float32, device=device)

    for ep in range(args.episodes):
        s_node, s_fm, s_mask = env.reset()
        done = False
        ep_reward = 0.0

        while not done:
            node_t = to_t(s_node).unsqueeze(0)
            fm_t = to_t(s_fm).unsqueeze(0)
            mask_t = torch.tensor(s_mask, dtype=torch.bool, device=device).unsqueeze(0)

            with torch.no_grad():
                f_vec = net.actor_f(node_t, fm_t)
                q = net.q_values(node_t, fm_t, f_vec)
                q = q.masked_fill(~mask_t, -1e9)

            feasible = np.where(s_mask)[0]
            if len(feasible) == 0:
                action = 0
            elif random.random() < eps:
                action = int(random.choice(feasible))
            else:
                action = int(torch.argmax(q, dim=1).item())

            f_value = float(f_vec[0, action].detach().cpu().item())

            (ns_node, ns_fm, ns_mask), reward, done = env.step(action, f_value)
            replay.push((s_node, s_fm, s_mask, action, reward, ns_node, ns_fm, ns_mask, done))
            ep_reward += reward

            s_node, s_fm, s_mask = ns_node, ns_fm, ns_mask

            if len(replay) >= args.batch_size:
                batch = replay.sample(args.batch_size)
                b_node, b_fm, b_mask, b_a, b_r, b_nnode, b_nfm, b_nmask, b_done = batch

                b_node = to_t(np.asarray(b_node))
                b_fm = to_t(np.asarray(b_fm))
                b_mask = torch.tensor(np.asarray(b_mask), dtype=torch.bool, device=device)
                b_a = torch.tensor(b_a, dtype=torch.long, device=device)
                b_r = to_t(np.asarray(b_r))
                b_nnode = to_t(np.asarray(b_nnode))
                b_nfm = to_t(np.asarray(b_nfm))
                b_nmask = torch.tensor(np.asarray(b_nmask), dtype=torch.bool, device=device)
                b_done = to_t(np.asarray(b_done, dtype=np.float32))

                f_cur = net.actor_f(b_node, b_fm)
                q_cur = net.q_values(b_node, b_fm, f_cur)
                q_a = q_cur.gather(1, b_a.view(-1, 1)).squeeze(1)

                with torch.no_grad():
                    f_next = target.actor_f(b_nnode, b_nfm)
                    q_next = target.q_values(b_nnode, b_nfm, f_next)
                    q_next = q_next.masked_fill(~b_nmask, -1e9)
                    max_next = q_next.max(dim=1).values
                    y = b_r + args.gamma * (1.0 - b_done) * max_next

                loss_q = F.smooth_l1_loss(q_a, y)

                opt_q.zero_grad()
                loss_q.backward()
                torch.nn.utils.clip_grad_norm_(net.parameters(), 5.0)
                opt_q.step()

                # actor-like update: choose f values that increase the best placement Q
                f_actor = net.actor_f(b_node, b_fm)
                q_actor = net.q_values(b_node, b_fm, f_actor)
                q_actor = q_actor.masked_fill(~b_mask, -1e9)
                loss_actor = -q_actor.max(dim=1).values.mean()

                opt_actor.zero_grad()
                loss_actor.backward()
                torch.nn.utils.clip_grad_norm_(net.parameters(), 5.0)
                opt_actor.step()

        eps = max(args.eps_end, eps * args.eps_decay)

        if (ep + 1) % args.target_update == 0:
            target.load_state_dict(net.state_dict())

        if (ep + 1) % args.log_every == 0:
            print(f"[TRAIN] episode={ep+1} reward={ep_reward:.3f} eps={eps:.3f}")

    # Greedy evaluation
    eval_env = GAHRLEnv(case, args)
    s_node, s_fm, s_mask = eval_env.reset()
    done = False

    while not done:
        node_t = to_t(s_node).unsqueeze(0)
        fm_t = to_t(s_fm).unsqueeze(0)
        mask_t = torch.tensor(s_mask, dtype=torch.bool, device=device).unsqueeze(0)

        with torch.no_grad():
            f_vec = net.actor_f(node_t, fm_t)
            q = net.q_values(node_t, fm_t, f_vec)
            q = q.masked_fill(~mask_t, -1e9)
            action = int(torch.argmax(q, dim=1).item())
            f_value = float(f_vec[0, action].detach().cpu().item())

        (s_node, s_fm, s_mask), reward, done = eval_env.step(action, f_value)

    result = simulate_standard(case, eval_env.assignment)
    result["assignment"] = eval_env.assignment
    result["allocations"] = eval_env.allocations
    result["summary"]["train_episodes"] = args.episodes
    result["summary"]["seed"] = args.seed
    result["summary"]["note"] = "GAHRL-like reimplementation with GCN/FM-style encoder, actor resource proxy, and dueling Q placement. Final ACT/AMS use the same simulator metric as other baselines."

    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)

    ap.add_argument("--episodes", type=int, default=80)
    ap.add_argument("--hidden", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--gamma", type=float, default=0.95)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--replay-size", type=int, default=20000)
    ap.add_argument("--target-update", type=int, default=10)
    ap.add_argument("--log-every", type=int, default=10)

    ap.add_argument("--eps-start", type=float, default=0.8)
    ap.add_argument("--eps-end", type=float, default=0.05)
    ap.add_argument("--eps-decay", type=float, default=0.96)

    ap.add_argument("--lambda-latency", type=float, default=0.5)
    ap.add_argument("--w-resource", type=float, default=0.30)
    ap.add_argument("--w-imbalance", type=float, default=0.20)
    ap.add_argument("--w-layer-hit", type=float, default=0.20)

    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--cpu", action="store_true")

    args = ap.parse_args()

    case = load_json(args.case)
    result = train_and_eval(case, args)
    save_json(result, args.out)
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
