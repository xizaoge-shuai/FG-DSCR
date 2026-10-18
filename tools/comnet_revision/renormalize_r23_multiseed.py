#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recalculate 20-seed paired COMNET J and 95% bootstrap CIs with heldout R2.3 refs.

Pure re-evaluation from archived *raw* D_s, CA_rate, frag_score.
No simulator execution. Never modify original analysis outputs.
Seeds, sizes, methods come from archived data and are strictly validated.
"""
from __future__ import annotations
import argparse,csv,hashlib,json,math,random,statistics
from collections import defaultdict
from pathlib import Path

EXPECTED_METHODS=("FG-DSCR-GC","ILR-SA","LRScheduler","GAHRL","ORR","LASA")
REF_FIELDS=("D","CA","Frag")
# Archived paired analyzer used manuscript-rounded denominators, not the full-precision historical means.
ARCHIVED_REF={"D":440.7696,"CA":0.77924,"Frag":0.180069}

def readcsv(p):
    with p.open(newline="",encoding="utf-8") as f:return list(csv.DictReader(f))

def writecsv(p,rows,fields):
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields)
        w.writeheader();w.writerows(rows)

def bootstrap(vals, label, repetitions=5000):
    if len(vals)<2:raise ValueError("Bootstrap needs at least two independent seeds")
    h=hashlib.sha256(label.encode()).digest()
    rng=random.Random(int.from_bytes(h[:8],"big"))
    n=len(vals)
    simulated=sorted(statistics.mean(vals[rng.randrange(n)] for _ in range(n)) for _ in range(repetitions))
    def percentile(p):
        idx=(len(simulated)-1)*p
        lo=math.floor(idx);hi=math.ceil(idx)
        return simulated[lo]+(simulated[hi]-simulated[lo])*(idx-lo)
    return percentile(.025),percentile(.975)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--archive",type=Path,default=Path("results/comnet_revision/multiseed_archived"))
    ap.add_argument("--calibration",type=Path,default=Path("results/comnet_revision/reproducibility_r23/heldout_calibration/heldout_calibration_references.json"))
    ap.add_argument("--out-dir",type=Path,default=Path("results/comnet_revision/reproducibility_r23/multiseed_newJ"))
    args=ap.parse_args()
    archive=args.archive.resolve();calibration=args.calibration.resolve();out=args.out_dir.resolve()
    raws=readcsv(archive/"per_seed_allrequest_metrics.csv")
    refs=json.loads(calibration.read_text(encoding="utf-8"))
    if refs.get("status")!="NEW HELD-OUT CALIBRATION, NOT ORIGINAL ARTICLE REFERENCE":
        raise RuntimeError("Expected frozen independent held-out calibration metadata")
    new_ref=refs["reference_values"];old_ref=refs["old_article_reference_values"]
    for key in REF_FIELDS:
        for obj in (new_ref,old_ref):
            val=float(obj[key])
            if not math.isfinite(val) or val<=0:raise RuntimeError(f"Invalid {key} reference: {val}")
    if len(raws)<2:raise RuntimeError("Missing multiseed raw metrics")
    keyed={};methods=set();sizes=set();seeds=set()
    for row in raws:
        key=(int(row["seed"]),int(row["requests"]),row["method"])
        if key in keyed:raise RuntimeError("Duplicate independent seed/scale/method: "+str(key))
        if row["method"] not in EXPECTED_METHODS:raise RuntimeError("Unexpected method "+row["method"])
        nums={"D":float(row["D_s"]),"CA":float(row["CA_rate"]),"Frag":float(row["frag_score"])}
        if any(not math.isfinite(x) or x<0 for x in nums.values()):raise RuntimeError("Bad raw metrics "+str(key))
        old=sum(nums[k]/float(old_ref[k]) for k in REF_FIELDS)
        new=sum(nums[k]/float(new_ref[k]) for k in REF_FIELDS)
        recorded=float(row["J"])
        archived=sum(nums[k]/ARCHIVED_REF[k] for k in REF_FIELDS)
        if not math.isclose(archived,recorded,rel_tol=1e-8,abs_tol=1e-8):
            raise RuntimeError("Archived J fails original-reference check "+str(key))
        keyed[key]={"J_original":old,"J_heldout":new,**nums}
        seeds.add(key[0]);sizes.add(key[1]);methods.add(key[2])
    if methods!=set(EXPECTED_METHODS):raise RuntimeError("Missing baseline methods")
    if set(seeds)&set(map(int,refs["seeds"])):
        raise RuntimeError("Overlap between confirmatory paired test and heldout calibration seeds")
    if len(seeds)<2:raise RuntimeError("Require at least 2 independent seeds")
    expected={(seed,n,m) for seed in seeds for n in sizes for m in EXPECTED_METHODS}
    if set(keyed)!=expected:raise RuntimeError(f"Non-complete paired matrix; missing={len(expected-set(keyed))} extra={len(set(keyed)-expected)}")
    details=[]
    for (seed,n,method),rec in sorted(keyed.items()):
        details.append({"seed":seed,"requests":n,"method":method,**rec})
    writecsv(out/"per_seed_newJ.csv",details,
             ["seed","requests","method","J_original","J_heldout","D","CA","Frag"])
    summaries=[]
    for n in sorted(sizes):
        for m in EXPECTED_METHODS:
            vals=[keyed[seed,n,m]["J_heldout"] for seed in sorted(seeds)]
            low,high=bootstrap(vals,f"newJ_abs_{n}_{m}")
            summaries.append({"requests":n,"method":m,"mean_J_heldout":statistics.mean(vals),
                              "sd_J_heldout":statistics.stdev(vals),"mean_CI95_low":low,
                              "mean_CI95_high":high,"seeds":len(seeds)})
    writecsv(out/"method_mean_ci95.csv",summaries,list(summaries[0]))
    paired=[]
    for scope in [*sorted(sizes),"ALL_SCALES_AVG_PER_SEED"]:
        for m in EXPECTED_METHODS[1:]:
            deltas=[];improvements=[]
            for seed in sorted(seeds):
                if isinstance(scope,int):
                    fg=keyed[seed,scope,"FG-DSCR-GC"]["J_heldout"]
                    bm=keyed[seed,scope,m]["J_heldout"]
                else:
                    fg=statistics.mean(keyed[seed,n,"FG-DSCR-GC"]["J_heldout"] for n in sizes)
                    bm=statistics.mean(keyed[seed,n,m]["J_heldout"] for n in sizes)
                d=bm-fg
                deltas.append(d)
                improvements.append(100*d/bm if bm else float("nan"))
            if not all(math.isfinite(x) for x in improvements):
                raise RuntimeError("Nonfinite improvement")
            low,high=bootstrap(deltas,f"newJ_paired_diff_{scope}_{m}")
            plow,phigh=bootstrap(improvements,f"newJ_paired_pct_{scope}_{m}")
            paired.append({
                "requests":scope,"baseline":m,"paired_seeds":len(seeds),
                "mean_deltaJ_baseline_minus_FALCONS":statistics.mean(deltas),
                "deltaJ_CI95_low":low,"deltaJ_CI95_high":high,
                "mean_paired_improvement_pct":statistics.mean(improvements),
                "improvement_pct_CI95_low":plow,"improvement_pct_CI95_high":phigh,
                "FALCONS_better_seeds":sum(d>0 for d in deltas),
                "CI_excludes_zero":int(low>0 or high<0)
            })
    writecsv(out/"paired_newJ_ci95.csv",paired,list(paired[0]))
    lines=[
        "# R2.3 独立校准尺度下的 20 种子配对统计","",
        f"- 校准种子：{refs['seeds']}；测试种子数：{len(seeds)}；测试规模：{sorted(sizes)}。",
        "- 原始 D、CA、Frag 不变，只更换 J 分母；没有重新跑算法。",
        "- 同一随机种子内所有方法使用相同请求输入；不同规模在种子内相关。",
        "- 统计单位是种子，跨规模先对每个种子求平均，再做 5000 次配对 bootstrap。",
        "- 本结果不能等同于生产系统实测。",
        "","## FALCONS 相对各 Baseline 的配对差值","",
        "| 规模 | Baseline | ΔJ（Baseline - FALCONS） | 95% CI | 改善 | 95% CI | 胜出种子 |",
        "|---|---|---:|---|---:|---|---:|"
    ]
    for r in paired:
        lines.append(f"| {r['requests']} | {r['baseline']} | "
                     f"{r['mean_deltaJ_baseline_minus_FALCONS']:.4f} | "
                     f"[{r['deltaJ_CI95_low']:.4f}, {r['deltaJ_CI95_high']:.4f}] | "
                     f"{r['mean_paired_improvement_pct']:.2f}% | "
                     f"[{r['improvement_pct_CI95_low']:.2f}%, {r['improvement_pct_CI95_high']:.2f}%] | "
                     f"{r['FALCONS_better_seeds']}/{r['paired_seeds']} |")
    lines+=["","## 解读限制","",
            "- 此处改善百分比是“先计算每个种子的配对改善百分比，再求平均”，不同于以两种方法九规模平均 J 相除得到的百分比。",
            "- 是否存在统计显著差异请查看 CI 是否跨 0；这里只给出自助抽样区间，不可声称随机轨迹采集独立。",
            "- 如更新论文，应使用同一固定参考值重算所有涉及 J 的表、图、显著性检验。",
            "- 旧参考值与新参考值不应混用；历史 Fig.8 的数字不能直接称为独立校准版本。"]
    (out/"paired_newJ_report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    manifest={"calibration_file":str(calibration),"archive":str(archive),
              "heldout_references":new_ref,"test_seed_count":len(seeds),
              "test_seeds":sorted(seeds),"test_request_scales":sorted(sizes),
              "rows":len(details),"method_count":len(methods)}
    (out/"analysis_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("Re-normalized",len(details),"archived 20-seed test rows; calibration refs",new_ref)
    print("Saved",out/"paired_newJ_report.md")
    for r in paired:
        if r["requests"]=="ALL_SCALES_AVG_PER_SEED":
            print(f"ALL vs {r['baseline']}: DeltaJ={r['mean_deltaJ_baseline_minus_FALCONS']:.4f}, "
                  f"CI=[{r['deltaJ_CI95_low']:.4f},{r['deltaJ_CI95_high']:.4f}], "
                  f"paired%={r['mean_paired_improvement_pct']:.2f}% "
                  f"wins={r['FALCONS_better_seeds']}/{r['paired_seeds']}")
if __name__=="__main__":main()
