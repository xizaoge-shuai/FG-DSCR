import json
import hashlib
import math
from pathlib import Path

ROOT = Path("results/drtp/k8s_same_scale")

def md5_obj(x):
    return hashlib.md5(json.dumps(x, sort_keys=True).encode()).hexdigest()

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def count_json(path):
    p = Path(path)
    return len(list(p.glob("*.json"))) if p.exists() else -1

def check_count(name, path, expected):
    n = count_json(path)
    status = "PASS" if n == expected else "FAIL"
    print(f"[{status}] {name}: {n}/{expected} -> {path}")

def check_prefix_sequence(name, paths):
    prev = None
    prev_path = None
    bad = 0
    missing = 0

    for p in paths:
        p = Path(p)
        if not p.exists():
            missing += 1
            prev = None
            prev_path = None
            continue

        cont = load_json(p).get("containers", [])

        if prev is not None:
            ok = len(cont) >= len(prev) and all(md5_obj(prev[i]) == md5_obj(cont[i]) for i in range(len(prev)))
            if not ok:
                bad += 1
                print(f"  [BAD_PREFIX] {prev_path} -> {p}")

        prev = cont
        prev_path = p

    status = "PASS" if bad == 0 and missing == 0 else "FAIL"
    print(f"[{status}] {name}: bad_prefix={bad}, missing={missing}")

def table_data_rows(path, first_header_name):
    p = Path(path)
    if not p.exists():
        print(f"[MISSING] {path}")
        return []

    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| "):
            continue
        if line.startswith("|---"):
            continue
        if line.startswith(f"| {first_header_name} |"):
            continue
        parts = [x.strip() for x in line.strip().strip("|").split("|")]
        if parts:
            rows.append(parts)
    return rows

def compare_table_rows(name, a, b, first_header_name):
    ra = table_data_rows(a, first_header_name)
    rb = table_data_rows(b, first_header_name)
    same = sum(x == y for x, y in zip(ra, rb))
    total = min(len(ra), len(rb))
    status = "PASS" if same < total else "FAIL"
    print(f"[{status}] {name}: same_rows={same}/{total}")

def check_future_not_all_half(path):
    p = Path(path)
    if not p.exists():
        print(f"[MISSING] {path}")
        return

    vals = []
    for row in table_data_rows(p, "cache_mb"):
        for x in row[1:]:
            try:
                vals.append(float(x))
            except Exception:
                pass

    all_half = bool(vals) and all(abs(x - 0.5) < 1e-12 for x in vals)
    status = "FAIL" if all_half else "PASS"
    print(f"[{status}] Fig.2 future_t not all 0.5: all_half={all_half}, min={min(vals) if vals else None}, max={max(vals) if vals else None}")

def check_fig5_formula(path):
    p = Path(path)
    if not p.exists():
        print(f"[MISSING] {path}")
        return

    lines = p.read_text(encoding="utf-8").splitlines()
    header = None
    checked = 0
    bad = 0

    for line in lines:
        if line.startswith("| requests |"):
            header = [x.strip() for x in line.strip().strip("|").split("|")]
            continue
        if not line.startswith("| ") or line.startswith("|---"):
            continue
        if header is None:
            continue

        parts = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(parts) != len(header):
            continue

        d = dict(zip(header, parts))
        try:
            obj = float(d["Obj_Final"])
            delay = float(d["Delay_all_norm"])
            ca = float(d["CA_norm"])
            frag = float(d["Frag_norm"])
        except Exception:
            continue

        checked += 1
        # 表里是三位小数，允许 0.01 以内舍入误差
        if abs(obj - (delay + ca + frag)) > 0.015:
            bad += 1
            print("[BAD_FORMULA]", d.get("requests"), d.get("method"), obj, delay + ca + frag)

    status = "PASS" if checked > 0 and bad == 0 else "FAIL"
    print(f"[{status}] Fig.5 Obj_Final = Delay_all_norm + CA_norm + Frag_norm: checked={checked}, bad={bad}")

def scan_fgor():
    dirs = [
        ROOT / "tables_process_norm_fig1_v2_prefix",
        ROOT / "tables_process_norm_fig2_v2_prefix",
        ROOT / "tables_fig3_scale_prefix",
        ROOT / "tables_norm_final_fig4_equalmean_prefix",
        ROOT / "tables_fig4_raw_prefix",
        ROOT / "tables_allrequest_fig5_edgeca300_prefix",
    ]

    hits = []
    for d in dirs:
        if not d.exists():
            continue
        for p in d.rglob("*.md"):
            text = p.read_text(encoding="utf-8", errors="ignore")
            if "FG-orig" in text:
                hits.append(str(p))

    status = "PASS" if not hits else "WARN"
    print(f"[{status}] FG-orig residue files: {len(hits)}")
    for h in hits[:20]:
        print("  ", h)

def scan_old_path_in_scripts():
    pats = [
        "fig5_overall_no_fgor",
        "tables_process_norm_fig1/",
        "tables_process_norm_fig2/",
        "fig2_cache/",
        "fig3_scale/",
        "fig4_network/",
        "tables_allrequest_fig5_edgeca300/",
    ]

    files = list(Path("tools").glob("*.py")) + list(Path("scripts").rglob("*.py")) + list(Path("scripts").rglob("*.sh"))
    hits = []

    for p in files:
        try:
            s = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pat in pats:
            if pat in s and "prefix" not in s:
                hits.append((str(p), pat))

    status = "PASS" if not hits else "WARN"
    print(f"[{status}] possible old non-prefix paths in scripts: {len(hits)}")
    for p, pat in hits[:40]:
        print(f"  {p}: {pat}")

print("\n========== 1. Result count check ==========")
check_count("Fig.5 prefix", ROOT / "fig5_overall_prefix", 54)
check_count("Fig.2 cache prefix", ROOT / "fig2_cache_prefix", 486)
check_count("Fig.3 scale prefix", ROOT / "fig3_scale_prefix", 256)
check_count("Fig.4 network equalmean prefix", ROOT / "fig4_network_equalmean_prefix", 324)

print("\n========== 2. Prefix case check ==========")
REQS_9 = [200,300,400,500,600,700,800,900,1000]
REQS_8 = [200,400,600,800,1000,1200,1500,2000]
CACHES = [0,128,256,384,512,640,768,896,1024]

check_prefix_sequence(
    "Fig.1/Fig.5 cache1024 prefix",
    [f"cases/drtp_cache_only_sweep_88_prefix/drtp_img88_cacheonly_1024mb_{r}.json" for r in REQS_9]
)

for c in CACHES:
    check_prefix_sequence(
        f"Fig.2 cache={c} prefix",
        [f"cases/drtp_cache_only_sweep_88_prefix/drtp_img88_cacheonly_{c}mb_{r}.json" for r in REQS_9]
    )

for cat in [16,50,68,88]:
    for edge in [4,6,8,10,12,14,16,18]:
        check_prefix_sequence(
            f"Fig.3 cat={cat}, edge={edge} prefix",
            [f"cases/drtp_scale_prefix/drtp_img{cat}_nodes{edge}_cache1024mb_{r}.json" for r in REQS_8]
        )

fig4_case_root = ROOT / "cases" / "fig4_network_equalmean_prefix"
if fig4_case_root.exists():
    for env in ["homo_good", "homo_bad", "hetero_good", "hetero_bad"]:
        for c in CACHES:
            check_prefix_sequence(
                f"Fig.4 env={env}, cache={c} prefix",
                [fig4_case_root / f"case_{env}_cache{c}_req{r}.json" for r in REQS_9]
            )
else:
    print(f"[WARN] Fig.4 case root not found: {fig4_case_root}")

print("\n========== 3. Fig.1/Fig.2 term sanity ==========")
compare_table_rows(
    "Fig.1 delay_term vs load_term",
    ROOT / "tables_process_norm_fig1_v2_prefix/fig1_process_delay_term_matrix.md",
    ROOT / "tables_process_norm_fig1_v2_prefix/fig1_process_load_term_matrix.md",
    "requests"
)

compare_table_rows(
    "Fig.2 reuse_t vs pull_t_saving",
    ROOT / "tables_process_norm_fig2_v2_prefix/fig2_process_reuse_t_norm_matrix.md",
    ROOT / "tables_process_norm_fig2_v2_prefix/fig2_process_pull_t_saving_norm_matrix.md",
    "cache_mb"
)

check_future_not_all_half(
    ROOT / "tables_process_norm_fig2_v2_prefix/fig2_process_future_t_norm_matrix.md"
)

print("\n========== 4. Fig.5 formula check ==========")
check_fig5_formula(
    ROOT / "tables_allrequest_fig5_edgeca300_prefix/fig5_allrequest_detail.md"
)

print("\n========== 5. FG-orig residue check ==========")
scan_fgor()

print("\n========== 6. Old path usage check ==========")
scan_old_path_in_scripts()
