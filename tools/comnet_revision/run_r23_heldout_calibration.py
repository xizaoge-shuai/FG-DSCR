#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FALCONS/COMNET reviewer R2.3: independent holdout calibration workloads.

Goals:
  * Fresh PRNG-seed workloads from archived DRTP profile + 88 real layer catalog,
    NOT bootstrap resamples of the original 1000 request list.
  * Different draws from original seed=42 and paired multiseed seeds=30101..30120.
  * Matched calibration cases for the six manuscript comparison methods.
  * Reuse the ORIGINAL six method launcher and all-request D/CA/Frag evaluator.
  * Freeze independent calibration means and compare J(old) vs J(new) on
    the ORIGINAL 54 article rows. NEVER overwrite original tables or inputs.

Three stages: --mode prepare | run | analyze. Idempotent, resumable.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path

METHODS=("fg","ilrsa","lrs","gahrl","orr","lasa")
METHOD_NAMES={"fg":"FG-DSCR-GC","ilrsa":"ILR-SA","lrs":"LRScheduler",
              "gahrl":"GAHRL","orr":"ORR","lasa":"LASA"}
HISTORIC_REF={"D":440.7696111111111,"CA":0.7792425925925927,"Frag":0.1800691666666667}
HISTORIC_EVAL=Path("results/drtp/k8s_same_scale/tables_allrequest_fig5_edgeca300_prefix/fig5_allrequest_detail.md")
ORIGINAL_CASE=Path("cases/drtp_cache_only_sweep_88_prefix/drtp_img88_cacheonly_1024mb_1000.json")


def sha(path: Path)->str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for buf in iter(lambda:f.read(4*1048576),b""):
            h.update(buf)
    return h.hexdigest()


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_no_replace(path: Path,obj):
    body=json.dumps(obj,ensure_ascii=False,indent=2,sort_keys=True)+"\n"
    if path.exists():
        if path.read_text(encoding="utf-8")!=body:
            raise RuntimeError(f"Existing output differs; refusing overwrite: {path}")
    else:
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(body,encoding="utf-8")


def writecsv(path: Path, rows, fields):
    with path.open("w",newline="",encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def markdown_rows(path: Path):
    header=None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells=[c.strip() for c in line.strip().strip("|").split("|")]
        if {"requests","method","Delay_all","CA_rate","frag_score"}.issubset(set(cells)):
            header=cells
        elif header and len(cells)==len(header) and cells[0].isdigit():
            yield dict(zip(header,cells))


def parse_env(path: Path):
    result={}
    for ln in path.read_text(encoding="utf-8").splitlines():
        m=re.match(r"^\s*export\s+([A-Z_][A-Z0-9_]*)=(.+?)\s*$",ln)
        if m:
            result[m.group(1)]=m.group(2).strip().strip('"').strip("'")
    required={"FG_INIT_ORDER","FG_LAMBDA_FRAG","FG_LAMBDA_AFF","FG_GREEDY_LOAD_FACTOR"}
    if not required.issubset(result):
        raise ValueError(f"Selected historical parameter fields missing: {required-set(result)}")
    return result


def config(args):
    data_files={
        "profile":Path("data/stats/drtp_stats/drtp_profile.json"),
        "catalog":Path("data/catalog/image_catalog_large_v2.json"),
        "generator":Path("scripts/build_fg_case_from_catalog_and_trace.py"),
        "launcher":Path("scripts/k8s_same_scale/run_one.sh"),
        "evaluator":Path("tools/summarize_fig5_allrequest_latency.py"),
        "params":Path("results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env"),
        "original_case":ORIGINAL_CASE,
    }
    paths={k:args.repo/v for k,v in data_files.items()}
    for k,p in paths.items():
        if not p.is_file():raise FileNotFoundError(f"{k}: {p}")
    if len(load(paths["catalog"]))!=88:raise ValueError("Expected 88 real image catalog entries")
    if len(load(paths["original_case"])["containers"])!=1000:
        raise ValueError("Expected original 1000-request case")
    meta=load(paths["original_case"]).get("meta",{})
    if int(meta.get("seed",-1)) in args.seeds:
        raise ValueError("Holdout seeds overlap original case seed")
    if set(args.seeds)&set(range(30101,30121)):
        raise ValueError("Holdout seeds overlap original 20-seed experiment")
    if len(set(args.seeds))!=len(args.seeds) or len(set(args.sizes))!=len(args.sizes):
        raise ValueError("Duplicate seed or size")
    if not args.sizes or min(args.sizes)<1 or max(args.sizes)>1000:
        raise ValueError("Sizes must be in 1..1000")
    if len(args.seeds)<2:raise ValueError("At least two held-out seeds required")
    params=parse_env(paths["params"])
    return {
        "purpose":"COMNET R2.3 held-out calibration, NEW experiment, not original article calibration",
        "seeds":args.seeds,
        "sizes":args.sizes,
        "methods":list(METHODS),
        "catalog_count":88,
        "nodes":8,
        "cache_capacity_mb":1024,
        "initial_cache_ratio":0.0,
        "CA_PROVISION_SEC":300.0,
        "CA_START_SEC":0.0,
        "LAMBDA_CA":1000.0,
        "python_hash_seed":0,
        "selected_fg_env":params,
        "input_files":{k:str(v) for k,v in data_files.items()},
        "input_sha256":{k:sha(p) for k,p in paths.items()},
        "source_note":"Distinct seeded samples using existing DRTP popularity profile and Docker Hub OCI image catalog, not independently captured data center traces",
        "calibration_statistic":"Global arithmetic mean of raw D/CA_rate/frag_score across all seed-scale-method records; all three must be >0",
        "historic_normalization":"Historical original-54-row references retained; new references compared side-by-side only",
    }


def record_log(argv, log, env):
    log.parent.mkdir(parents=True,exist_ok=True)
    print("[RUN]", " ".join(map(str,argv)), "LOG",log,flush=True)
    with log.open("w",encoding="utf-8") as f:
        r=subprocess.run(argv,cwd=env["_FG_ROOT"],env={k:v for k,v in env.items() if k!="_FG_ROOT"},
                         stdout=f,stderr=subprocess.STDOUT,check=False)
        f.write(f"\nCALIBRATION_EXEC_RETURN={r.returncode}\n")
    if r.returncode!=0:
        raise RuntimeError(f"Command failed exit={r.returncode}; inspect log {log}")


def casefile(args,seed,n):
    return args.out_dir/"cases"/f"seed{seed}"/f"drtp_img88_cacheonly_1024mb_{n}.json"


def resultfile(args,seed,n,method):
    return args.out_dir/"raw"/f"seed{seed}"/f"{method}_req{n}.json"


def prepare(args):
    env=dict(os.environ);env["_FG_ROOT"]=str(args.repo)
    c=config(args)
    write_json_no_replace(args.out_dir/"calibration_config.json",c)
    original=load(args.repo/ORIGINAL_CASE)
    originals=original["containers"]
    for seed in args.seeds:
        p=casefile(args,seed,1000)
        if not p.exists():
            cmd=[sys.executable,"scripts/build_fg_case_from_catalog_and_trace.py",
                 "--catalog","data/catalog/image_catalog_large_v2.json",
                 "--trace-profile","data/stats/drtp_stats/drtp_profile.json",
                 "--sizes","1000","--seed",str(seed),"--num-nodes","8",
                 "--repo-capacity-mb","1024","--warm-cache-ratio","0",
                 "--out-prefix",str(p).removesuffix("_1000.json")]
            record_log(cmd,args.out_dir/"logs"/f"seed{seed}"/"generate.log",env)
        obj=load(p)
        if obj.get("meta",{}).get("seed")!=seed or len(obj.get("containers",[]))!=1000:
            raise RuntimeError("Unexpected case seed or length "+str(p))
        if obj["containers"]==originals:
            raise RuntimeError("Calibration sample exactly equals main article workload: "+str(seed))
        if any(n["initial_cache"] for n in obj["nodes"]):
            raise RuntimeError("Expected empty initial simulation caches")
        for n in args.sizes:
            dst=casefile(args,seed,n)
            sliced={k:v for k,v in obj.items() if k not in ("containers","meta")}
            sliced["containers"]=obj["containers"][:n]
            sliced["meta"]={**obj["meta"],"num_containers":n,
                            "calibration_holdout":True,
                            "calibration_seed":seed,
                            "source_1000_sha256":sha(p),
                            "calibration_protocol":"DRTP weighted synthetic draws; seeded, frozen"}
            if dst!=p:
                write_json_no_replace(dst,sliced)
        print(f"[PREPARED] seed={seed}, case1000={sha(p)}, sizes={args.sizes}",flush=True)
    print("PREPARE COMPLETE",args.out_dir,flush=True)


def validate_setup(args):
    planned=config(args)
    saved=args.out_dir/"calibration_config.json"
    if not saved.is_file():
        raise RuntimeError("Missing calibration config; run --mode prepare first")
    if load(saved)!=planned:
        raise RuntimeError("Reproducibility contract changed (input hash or config). Do not mix experimental versions.")
    for seed in args.seeds:
        master=casefile(args,seed,1000)
        if not master.is_file():raise FileNotFoundError(master)
        source=load(master)["containers"]
        for n in args.sizes:
            p=casefile(args,seed,n)
            if not p.exists():raise FileNotFoundError(p)
            target=load(p)["containers"]
            if target!=source[:n]:
                raise RuntimeError("Cases are NOT matching seeded prefix workloads: "+str(p))


def run(args):
    validate_setup(args)
    env=dict(os.environ)
    env.update(parse_env(args.repo/Path("results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env")))
    env.update({"_FG_ROOT":str(args.repo),"PYTHONHASHSEED":"0","LAMBDA_CA":"1000"})
    for seed in args.seeds:
        for n in args.sizes:
            c=casefile(args,seed,n)
            for method in METHODS:
                dest=resultfile(args,seed,n,method)
                if dest.is_file():
                    try:
                        saved=load(dest)
                        if isinstance(saved.get("summary"),dict) and isinstance(saved.get("assignment"),dict):
                            print(f"[SKIP] seed={seed} n={n} method={method}",flush=True)
                            continue
                    except (ValueError,TypeError):pass
                    raise RuntimeError("Existing incomplete result; inspect but do not overwrite: "+str(dest))
                dest.parent.mkdir(parents=True,exist_ok=True)
                cmd=["bash","scripts/k8s_same_scale/run_one.sh",method,str(c),str(dest)]
                record_log(cmd,args.out_dir/"logs"/f"seed{seed}"/f"{method}_n{n}.log",env)
                result=load(dest)
                if not isinstance(result.get("summary"),dict) or not isinstance(result.get("assignment"),dict):
                    raise RuntimeError("Missing proper output: "+str(dest))
                print(f"[DONE] seed={seed} n={n} method={method}",flush=True)
    print("ALL CALIBRATION RUNS COMPLETE",flush=True)


def analyze(args):
    validate_setup(args)
    env=dict(os.environ)
    env.update({"_FG_ROOT":str(args.repo),"PYTHONHASHSEED":"0",
                "CA_PROVISION_SEC":"300","CA_START_SEC":"0"})
    records=[]
    for seed in args.seeds:
        evalout=args.out_dir/"evaluated"/f"seed{seed}"
        detail=evalout/"fig5_allrequest_detail.md"
        expected={"fg":set(), "ilrsa":set()}
        for n in args.sizes:
            for method in METHODS:
                p=resultfile(args,seed,n,method)
                if not p.is_file():raise FileNotFoundError(p)
        if not detail.is_file():
            env.update({"CASE_DIR":str((args.out_dir/"cases"/f"seed{seed}").resolve()),
                        "FIG5_RES_DIR":str((args.out_dir/"raw"/f"seed{seed}").resolve()),
                        "OUT_DIR":str(evalout.resolve())})
            record_log([sys.executable,"tools/summarize_fig5_allrequest_latency.py"],
                       args.out_dir/"logs"/f"seed{seed}"/"evaluate.log",env)
        rows=list(markdown_rows(detail))
        required={(n, METHOD_NAMES[m]) for n in args.sizes for m in METHODS}
        observed={(int(x["requests"]),x["method"]) for x in rows}
        if observed!=required or len(rows)!=len(required):
            raise RuntimeError(f"Evaluator rows mismatch seed={seed}, missing={required-observed}, extra={observed-required}")
        for row in rows:
            if float(row.get("missing_edge_time",0))>0:
                raise RuntimeError("Missing edge completion observations seed="+str(seed))
            records.append({"seed":seed,"requests":int(row["requests"]),
                            "method":row["method"],"D":float(row["Delay_all"]),
                            "CA_rate":float(row["CA_rate"]),
                            "Frag":float(row["frag_score"])})
    refs={"D":statistics.mean(r["D"] for r in records),
          "CA":statistics.mean(r["CA_rate"] for r in records),
          "Frag":statistics.mean(r["Frag"] for r in records)}
    if any(not math.isfinite(x) or x<=0 for x in refs.values()):
        raise RuntimeError("Invalid independent calibration denominator: "+str(refs))
    writecsv(args.out_dir/"calibration_raw_rows.csv",records,
             ["seed","requests","method","D","CA_rate","Frag"])
    frozen={
        "status":"NEW HELD-OUT CALIBRATION, NOT ORIGINAL ARTICLE REFERENCE",
        "reference_values":refs,"old_article_reference_values":HISTORIC_REF,
        "number_of_seeds":len(args.seeds),
        "number_of_methods":len(METHODS),
        "number_of_request_scales":len(args.sizes),
        "number_of_raw_rows":len(records),
        "seeds":args.seeds,"sizes":args.sizes,
        "input_config_sha256":sha(args.out_dir/"calibration_config.json"),
        "raw_table_sha256":sha(args.out_dir/"calibration_raw_rows.csv"),
        "formula":"J = D/D_ref + CA_rate/CA_ref + Frag/Frag_ref",
        "warning":"Use only for a separately labeled re-normalized evaluation. Do not substitute in already submitted paper figures without recomputing all comparable J values.",
    }
    write_json_no_replace(args.out_dir/"heldout_calibration_references.json",frozen)
    oldfile=args.repo/HISTORIC_EVAL
    if not oldfile.exists():raise FileNotFoundError(oldfile)
    old=list(markdown_rows(oldfile))
    if len(old)!=54:raise RuntimeError("Original main article evaluation is not 54 rows")
    comparisons=[]
    for row in old:
        d=float(row["Delay_all"]);ca=float(row["CA_rate"]);fr=float(row["frag_score"])
        original=d/HISTORIC_REF["D"]+ca/HISTORIC_REF["CA"]+fr/HISTORIC_REF["Frag"]
        new=d/refs["D"]+ca/refs["CA"]+fr/refs["Frag"]
        comparisons.append({"requests":int(row["requests"]),"method":row["method"],
                            "D":d,"CA_rate":ca,"Frag":fr,
                            "J_article_original_reference":original,
                            "J_new_heldout_reference":new,
                            "J_difference":new-original})
    writecsv(args.out_dir/"article_J_reference_comparison.csv",comparisons,
             ["requests","method","D","CA_rate","Frag",
              "J_article_original_reference","J_new_heldout_reference","J_difference"])
    lines=[
        "# FALCONS R2.3 独立留出校准结果","",
        "这是一项新增的校准实验，不能追溯替代原论文既有归一化过程。","",
        f"- 校准种子：{args.seeds}（不含主实验 42 与补充实验 30101–30120）",
        f"- 请求规模：{args.sizes}；每个种子同一 1000 请求抽样的前缀",
        f"- 比较方法：{', '.join(METHOD_NAMES.values())}",
        f"- 原始校准记录：{len(records)} 条。",
        "- 图像层来自原 88 镜像目录；请求从保存的 DRTP 匿名仓库热度重新抽样。",
        "- 同一数据统计分布的独立随机样本，不是新的生产流量轨迹，也不是独立采集的数据集。",
        "- FGP 及各基线运行设置沿用归档的 run_one.sh 与 best_fg_edgeca_params.env。",
        "- CA provisioning penalty = 300 s；原始请求缓存初始空，缓存容量 1024 MB。","",
        "| 参考值 | 论文历史参考值 | 新的留出校准参考值 |",
        "|---|---:|---:|",
        f"| D_ref | {HISTORIC_REF['D']:.9f} | {refs['D']:.9f} |",
        f"| CA_ref | {HISTORIC_REF['CA']:.9f} | {refs['CA']:.9f} |",
        f"| Frag_ref | {HISTORIC_REF['Frag']:.9f} | {refs['Frag']:.9f} |","",
        "## 数据文件","",
        "- calibration_config.json：数据及程序哈希、方法和种子。",
        "- cases/：全部留出输入及元数据。",
        "- raw/：全部真实算法仿真输出。",
        "- evaluated/：统一原始评价。",
        "- calibration_raw_rows.csv：不归一化的 D、CA、Frag。",
        "- heldout_calibration_references.json：新参考值。",
        "- article_J_reference_comparison.csv：原稿 54 条记录的新旧 J 对照。","",
        "## 使用边界","",
        "原稿正文已使用历史参考值。若改用新参考值，需要基于原始 D、CA 和 Frag 重新计算正文和补充材料所有 J，检查方法排序和论文结论，不可只重算 FALCONS。"
    ]
    (args.out_dir/"calibration_report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print("CALIBRATION COMPLETE",json.dumps(refs,ensure_ascii=False),flush=True)
    print("REPORT",args.out_dir/"calibration_report.md")


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--mode",choices=("prepare","run","analyze"),required=True)
    p.add_argument("--repo",type=Path,default=Path.cwd())
    p.add_argument("--out-dir",type=Path,
                   default=Path("results/comnet_revision/reproducibility_r23/heldout_calibration"))
    p.add_argument("--seeds",nargs="+",type=int,default=[50101,50102,50103])
    p.add_argument("--sizes",nargs="+",type=int,default=[200,500,1000])
    a=p.parse_args()
    a.repo=a.repo.expanduser().resolve()
    a.out_dir=a.out_dir if a.out_dir.is_absolute() else a.repo/a.out_dir
    a.out_dir.mkdir(parents=True,exist_ok=True)
    if len(a.seeds)<2 or any(x<0 for x in a.seeds):
        p.error("Use at least two independent nonnegative seed values")
    if a.mode=="prepare":prepare(a)
    elif a.mode=="run":run(a)
    else:analyze(a)

if __name__=="__main__":main()
