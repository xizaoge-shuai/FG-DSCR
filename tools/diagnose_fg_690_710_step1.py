import json
from pathlib import Path
from collections import Counter

RES = Path("results/drtp/k8s_same_scale/fig5_overall_prefix_fine_690_710_step1")
TAB = Path("results/drtp/k8s_same_scale/tables_process_norm_fig1_v2_prefix_fine_690_710_step1")

def parse_matrix(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    header = None
    data = {}
    for line in lines:
        if line.startswith("| requests |"):
            header = [x.strip() for x in line.strip().strip("|").split("|")]
            continue
        if not line.startswith("| ") or line.startswith("|---") or header is None:
            continue
        parts = [x.strip() for x in line.strip().strip("|").split("|")]
        try:
            req = int(parts[0])
        except:
            continue
        # 这里只有 FG-DSCR-GC
        data[req] = float(parts[1])
    return data

delay = parse_matrix(TAB / "fig1_process_delay_term_matrix.md")
frag  = parse_matrix(TAB / "fig1_process_frag_term_matrix.md")
load  = parse_matrix(TAB / "fig1_process_load_term_matrix.md")

def load_result(req):
    p = RES / f"fg_prefix_req{req}.json"
    return json.load(open(p, "r", encoding="utf-8"))

print("| req | assigned | failed | fail_rate | ACT | AMS | downloaded | reuse_rate | objective | phase1_label | potential | delay | frag | load | node_counts |")
print("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---|")

for req in range(690, 711):
    r = load_result(req)
    s = r.get("summary", {})
    hist = r.get("phase1_history", [])
    last = hist[-1] if hist else {}

    counts = last.get("node_container_counts", {})
    counts_str = ",".join(f"{k}:{v}" for k, v in sorted(counts.items()))

    print(
        f"| {req} | "
        f"{s.get('num_assigned', '')} | "
        f"{s.get('num_failed', '')} | "
        f"{float(s.get('fail_rate', 0)):.4f} | "
        f"{float(s.get('ACT', 0)):.3f} | "
        f"{float(s.get('AMS', 0)):.3f} | "
        f"{float(s.get('downloaded_mb', 0)):.0f} | "
        f"{float(s.get('reuse_rate', 0)):.6f} | "
        f"{float(s.get('objective', 0)):.3f} | "
        f"{last.get('label', '')} | "
        f"{float(last.get('potential', 0)):.3f} | "
        f"{delay.get(req, float('nan')):.6f} | "
        f"{frag.get(req, float('nan')):.6f} | "
        f"{load.get(req, float('nan')):.6f} | "
        f"{counts_str} |"
    )
