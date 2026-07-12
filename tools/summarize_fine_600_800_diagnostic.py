import argparse
import json
import re
from pathlib import Path

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def parse_matrix(path, first_col):
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    header = None
    data = {}

    for line in lines:
        if line.startswith(f"| {first_col} |"):
            header = [x.strip() for x in line.strip().strip("|").split("|")]
            continue
        if not line.startswith("| ") or line.startswith("|---") or header is None:
            continue

        parts = [x.strip() for x in line.strip().strip("|").split("|")]
        try:
            x = int(parts[0])
        except Exception:
            continue

        for m, v in zip(header[1:], parts[1:]):
            try:
                data[(x, m)] = float(v)
            except Exception:
                pass

    return data

def method_name(name):
    if name.startswith("ilrsa"):
        return "ILR-SA"
    if name.startswith("lrs"):
        return "LRScheduler"
    if name.startswith("gahrl"):
        return "GAHRL"
    if name.startswith("orr"):
        return "ORR"
    if name.startswith("lasa"):
        return "LASA"
    if name.startswith("fg"):
        return "FG-DSCR-GC"
    return name

def get_assignment(result):
    return result.get("assignment") or result.get("assignments") or {}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=20)
    args = ap.parse_args()

    root = Path("results/drtp/k8s_same_scale")
    res_dir = root / f"fig5_overall_prefix_fine_600_800_step{args.step}"
    fig1_dir = root / f"tables_process_norm_fig1_v2_prefix_fine_600_800_step{args.step}"
    out_dir = root / f"tables_fine_600_800_step{args.step}"
    out_dir.mkdir(parents=True, exist_ok=True)

    delay = parse_matrix(fig1_dir / "fig1_process_delay_term_matrix.md", "requests")
    frag = parse_matrix(fig1_dir / "fig1_process_frag_term_matrix.md", "requests")
    load = parse_matrix(fig1_dir / "fig1_process_load_term_matrix.md", "requests")

    rows = []

    for p in sorted(res_dir.glob("*.json")):
        m = re.search(r"(.+)_prefix_req(\d+)\.json", p.name)
        if not m:
            continue

        method = method_name(m.group(1))
        req = int(m.group(2))

        r = load_json(p)
        s = r.get("summary", {})
        ass = get_assignment(r)

        edge_scheduled = sum(len(v) for v in ass.values())
        ca = req - edge_scheduled
        ca_rate = ca / req if req else 0.0

        rows.append({
            "requests": req,
            "method": method,
            "edge_scheduled": edge_scheduled,
            "CA": ca,
            "CA_rate": ca_rate,
            "delay_term": delay.get((req, method), None),
            "frag_term": frag.get((req, method), None),
            "load_term": load.get((req, method), None),
            "ACT": s.get("ACT", ""),
            "AMS": s.get("AMS", ""),
            "downloaded_mb": s.get("downloaded_mb", ""),
            "reuse_rate": s.get("reuse_rate", ""),
            "objective_raw": s.get("objective", ""),
        })

    methods = ["ILR-SA", "LRScheduler", "GAHRL", "ORR", "LASA", "FG-DSCR-GC"]
    rows.sort(key=lambda x: (x["requests"], methods.index(x["method"]) if x["method"] in methods else 99))

    lines = []
    lines.append("## Fine-grained 600-800 Diagnostic")
    lines.append("")
    lines.append("| requests | method | edge_scheduled | CA | CA_rate | delay_term | frag_term | load_term | ACT | AMS | downloaded_mb | reuse_rate | objective_raw |")
    lines.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

    for x in rows:
        def fmt(v, n=4):
            if v is None or v == "":
                return ""
            try:
                return f"{float(v):.{n}f}"
            except Exception:
                return str(v)

        lines.append(
            f"| {x['requests']} | {x['method']} | {x['edge_scheduled']} | {x['CA']} | "
            f"{x['CA_rate']:.4f} | {fmt(x['delay_term'])} | {fmt(x['frag_term'])} | {fmt(x['load_term'])} | "
            f"{fmt(x['ACT'],3)} | {fmt(x['AMS'],3)} | {fmt(x['downloaded_mb'],1)} | "
            f"{fmt(x['reuse_rate'],6)} | {fmt(x['objective_raw'],3)} |"
        )

    out = out_dir / "fine_600_800_diagnostic_detail.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written", out)

    # FG only
    fg_lines = [line for line in lines if line.startswith("| requests") or line.startswith("|---") or "| FG-DSCR-GC |" in line]
    fg_out = out_dir / "fine_600_800_fg_only.md"
    fg_out.write_text("\n".join(fg_lines) + "\n", encoding="utf-8")
    print("written", fg_out)

if __name__ == "__main__":
    main()
