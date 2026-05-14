from pathlib import Path

IN = Path("results/drtp/final_exp/final_plot_data/fig_cacheonly_dynamic_gain_components/cacheonly_dynamic_gain_components_long.md")
OUTDIR = Path("results/drtp/final_exp/final_plot_data/fig_cacheonly_dynamic_gain_components_per_cache_norm")
OUTDIR.mkdir(parents=True, exist_ok=True)

CACHE = [0,128,256,384,512,640,768,896,1024]
METHODS = ["LRScheduler", "GAHRL", "ORR", "LASA", "ILR-SA", "FG-DSCR-GC"]

rows = []

for line in IN.read_text(encoding="utf-8").splitlines():
    line=line.strip()
    if not line.startswith("|"):
        continue
    if line.startswith("|---") or "method" in line:
        continue

    parts=[x.strip() for x in line.strip("|").split("|")]
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

def norm_benefit(x, vals):
    lo=min(vals)
    hi=max(vals)
    if abs(hi-lo) < 1e-12:
        return 0.5
    return (x-lo)/(hi-lo)

def norm_cost_saving(x, vals):
    lo=min(vals)
    hi=max(vals)
    if abs(hi-lo) < 1e-12:
        return 0.5
    return (hi-x)/(hi-lo)

# 每个 cache 内，每个指标单独归一化
for cache in CACHE:
    sub=[r for r in rows if r["cache"] == cache]

    reuse_vals=[r["reuse_t"] for r in sub]
    future_vals=[r["future_t"] for r in sub]
    pull_vals=[r["pull_t"] for r in sub]
    evict_vals=[r["evict_t"] for r in sub]

    for r in sub:
        r["reuse_norm_pc"] = norm_benefit(r["reuse_t"], reuse_vals)
        r["future_norm_pc"] = norm_benefit(r["future_t"], future_vals)
        r["pull_save_norm_pc"] = norm_cost_saving(r["pull_t"], pull_vals)
        r["evict_save_norm_pc"] = norm_cost_saving(r["evict_t"], evict_vals)

        r["dynamic_gain_norm_pc"] = (
            r["reuse_norm_pc"]
            + r["future_norm_pc"]
            + r["pull_save_norm_pc"]
            + r["evict_save_norm_pc"]
        ) / 4.0

def get(method, cache):
    hit=[r for r in rows if r["method"] == method and r["cache"] == cache]
    return hit[0] if hit else None

def write_table(metric, title):
    path=OUTDIR / f"{metric}_lines.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# per-cache normalized {title}\n\n")
        f.write("> Direction: higher is better\n\n")
        f.write("| cache_mb | " + " | ".join(METHODS) + " |\n")
        f.write("|---:|" + "|".join(["---:"]*len(METHODS)) + "|\n")

        for cache in CACHE:
            vals=[]
            for method in METHODS:
                r=get(method, cache)
                vals.append("MISSING" if r is None else f"{r[metric]:.6f}")
            f.write("| " + str(cache) + " | " + " | ".join(vals) + " |\n")
    print("[OK]", path)

write_table("dynamic_gain_norm_pc", "Dynamic Gain Function")
write_table("reuse_norm_pc", "Reuse_t(u)")
write_table("future_norm_pc", "Future_t(u)")
write_table("pull_save_norm_pc", "Pull_t(u) saving")
write_table("evict_save_norm_pc", "Evict_t(u) saving")

long_path=OUTDIR / "per_cache_normalized_dynamic_gain_components_long.md"
with open(long_path, "w", encoding="utf-8") as f:
    f.write("| cache_mb | method | dynamic_gain_norm | reuse_norm | future_norm | pull_save_norm | evict_save_norm |\n")
    f.write("|---:|---|---:|---:|---:|---:|---:|\n")
    for cache in CACHE:
        for method in METHODS:
            r=get(method, cache)
            if r is None:
                continue
            f.write("| {} | {} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} |\n".format(
                cache,
                method,
                r["dynamic_gain_norm_pc"],
                r["reuse_norm_pc"],
                r["future_norm_pc"],
                r["pull_save_norm_pc"],
                r["evict_save_norm_pc"],
            ))
print("[OK]", long_path)

print("\n===== Dynamic Gain =====")
print((OUTDIR / "dynamic_gain_norm_pc_lines.md").read_text(encoding="utf-8"))

print("\n===== Reuse =====")
print((OUTDIR / "reuse_norm_pc_lines.md").read_text(encoding="utf-8"))

print("\n===== Future =====")
print((OUTDIR / "future_norm_pc_lines.md").read_text(encoding="utf-8"))

print("\n===== Pull Saving =====")
print((OUTDIR / "pull_save_norm_pc_lines.md").read_text(encoding="utf-8"))

print("\n===== Evict Saving =====")
print((OUTDIR / "evict_save_norm_pc_lines.md").read_text(encoding="utf-8"))
