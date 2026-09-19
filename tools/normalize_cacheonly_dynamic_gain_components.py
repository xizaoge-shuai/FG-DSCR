import os
from pathlib import Path

IN = Path("results/drtp/final_exp/final_plot_data/fig_cacheonly_dynamic_gain_components/cacheonly_dynamic_gain_components_long.md")
OUTDIR = Path("results/drtp/final_exp/final_plot_data/fig_cacheonly_dynamic_gain_components_normalized")
OUTDIR.mkdir(parents=True, exist_ok=True)

CACHE = [0,128,256,384,512,640,768,896,1024]
METHODS = ["LRScheduler", "GAHRL", "ORR", "LASA", "ILR-SA", "FG-DSCR-GC"]

rows = []

for line in IN.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line.startswith("|"):
        continue
    if line.startswith("|---") or "method" in line:
        continue

    parts = [x.strip() for x in line.strip("|").split("|")]
    if len(parts) != 9:
        continue

    method, cache, cases, dynamic_gain, reuse_t, future_t, pull_t_mb, pull_time_t_s, evict_t_mb = parts

    rows.append({
        "method": method,
        "cache": int(cache),
        "cases": int(cases),
        "dynamic_gain_raw": float(dynamic_gain),
        "reuse_t": float(reuse_t),
        "future_t": float(future_t),
        "pull_t": float(pull_t_mb),
        "pull_time_t": float(pull_time_t_s),
        "evict_t": float(evict_t_mb),
    })

def minmax_values(key):
    vals = [r[key] for r in rows]
    lo, hi = min(vals), max(vals)
    return lo, hi

def norm(x, lo, hi):
    if abs(hi - lo) < 1e-12:
        return 0.0
    return (x - lo) / (hi - lo)

bounds = {}
for key in ["reuse_t", "future_t", "pull_t", "evict_t", "pull_time_t", "dynamic_gain_raw"]:
    bounds[key] = minmax_values(key)

for r in rows:
    r["reuse_norm"] = norm(r["reuse_t"], *bounds["reuse_t"])
    r["future_norm"] = norm(r["future_t"], *bounds["future_t"])

    # Pull/Evict 是代价项，因此转成 saving/benefit，保证越高越好
    r["pull_save_norm"] = 1.0 - norm(r["pull_t"], *bounds["pull_t"])
    r["evict_save_norm"] = 1.0 - norm(r["evict_t"], *bounds["evict_t"])

    # 备用：如果想看“归一化代价”，这两个是越低越好
    r["pull_cost_norm"] = norm(r["pull_t"], *bounds["pull_t"])
    r["evict_cost_norm"] = norm(r["evict_t"], *bounds["evict_t"])

    r["pull_time_save_norm"] = 1.0 - norm(r["pull_time_t"], *bounds["pull_time_t"])

    # 推荐画这个：四个维度归一化后重新组合，避免 Future 项支配总分
    r["dynamic_gain_norm"] = (
        r["reuse_norm"]
        + r["future_norm"]
        + r["pull_save_norm"]
        + r["evict_save_norm"]
    ) / 4.0

    # 备用：原始 Dynamic Gain 的 min-max 归一化，不建议作为主图
    r["dynamic_gain_raw_minmax"] = norm(r["dynamic_gain_raw"], *bounds["dynamic_gain_raw"])

def write_lines(metric, title, higher_better=True):
    path = OUTDIR / f"{metric}_lines.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# normalized {title}\n\n")
        f.write(f"> Direction: {'higher is better' if higher_better else 'lower is better'}\n\n")
        f.write("| method | " + " | ".join(map(str, CACHE)) + " |\n")
        f.write("|---|" + "|".join(["---:"] * len(CACHE)) + "|\n")

        for method in METHODS:
            vals = []
            for cache in CACHE:
                hit = [r for r in rows if r["method"] == method and r["cache"] == cache]
                if not hit:
                    vals.append("MISSING")
                else:
                    vals.append(f"{hit[0][metric]:.6f}")
            f.write("| " + method + " | " + " | ".join(vals) + " |\n")

    print("[OK]", path)

write_lines("dynamic_gain_norm", "Dynamic Gain Function after component-wise normalization")
write_lines("reuse_norm", "Reuse_t(u)")
write_lines("future_norm", "Future_t(u)")
write_lines("pull_save_norm", "Pull_t(u) saving")
write_lines("evict_save_norm", "Evict_t(u) saving")

# 备用表：原始 cost 方向，越低越好
write_lines("pull_cost_norm", "Pull_t(u) normalized cost", higher_better=False)
write_lines("evict_cost_norm", "Evict_t(u) normalized cost", higher_better=False)
write_lines("dynamic_gain_raw_minmax", "Raw Dynamic Gain min-max normalization")

long_path = OUTDIR / "normalized_dynamic_gain_components_long.md"
with open(long_path, "w", encoding="utf-8") as f:
    f.write("| method | cache | cases | dynamic_gain_norm | reuse_norm | future_norm | pull_save_norm | evict_save_norm | pull_cost_norm | evict_cost_norm | raw_gain_minmax |\n")
    f.write("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
    for method in METHODS:
        for cache in CACHE:
            hit = [r for r in rows if r["method"] == method and r["cache"] == cache]
            if not hit:
                continue
            r = hit[0]
            f.write("| {} | {} | {} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} |\n".format(
                r["method"], r["cache"], r["cases"],
                r["dynamic_gain_norm"],
                r["reuse_norm"],
                r["future_norm"],
                r["pull_save_norm"],
                r["evict_save_norm"],
                r["pull_cost_norm"],
                r["evict_cost_norm"],
                r["dynamic_gain_raw_minmax"],
            ))

print("[OK]", long_path)

print("\n===== normalized Dynamic Gain =====")
print((OUTDIR / "dynamic_gain_norm_lines.md").read_text(encoding="utf-8"))

print("\n===== normalized Reuse =====")
print((OUTDIR / "reuse_norm_lines.md").read_text(encoding="utf-8"))

print("\n===== normalized Pull saving =====")
print((OUTDIR / "pull_save_norm_lines.md").read_text(encoding="utf-8"))
