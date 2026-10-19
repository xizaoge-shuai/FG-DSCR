#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only stage timings + actual kubelet CPU/MEM for official OCI trials.

Reports FGP, DCO, API acknowledgement, image-pull events, Pod startup,
controller-observed ACT/P95/D, and telemetry sample peaks. No physical PGDSF.
"""
from __future__ import annotations
import argparse,csv,json,math,pathlib,re,statistics,sys
from collections import defaultdict

def readjson(p):
    return json.loads(p.read_text(encoding="utf-8"))

def readcsv(p):
    if not p.is_file():return []
    with p.open(newline="",encoding="utf-8") as f:
        return list(csv.DictReader(f))

def avg(seq):
    v=[]
    for x in seq:
        try:
            if x is not None and x!="" and math.isfinite(float(x)):v.append(float(x))
        except (TypeError,ValueError):continue
    return statistics.mean(v) if v else None

def display(x):
    return "-" if x is None else f"{x:.3f}"

def writecsv(p,data):
    if not data:return
    with p.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(data[0]))
        w.writeheader();w.writerows(data)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--suite-dir",required=True,type=pathlib.Path)
    args=parser.parse_args()
    suite=args.suite_dir.resolve()
    sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
    from export_k8s_real_paper_metrics import collect
    config=readjson(suite/"suite_config.json")
    catalog=readjson(pathlib.Path(config["catalog"]))
    metrics,_=collect(suite/"runs",catalog)
    byrun={r["run_id"]:r for r in metrics}
    data=[]
    issues=[]
    for trial in readcsv(suite/"trials.csv"):
        if trial["exit_code"]!="0":
            issues.append("Nonzero exit: "+trial["log_path"])
            continue
        log=pathlib.Path(trial["log_path"])
        if not log.is_file():
            issues.append("Log missing: "+str(log));continue
        ids=re.findall(r"^RUN (oci-[a-f0-9]+)\b",
            log.read_text(encoding="utf-8",errors="replace"),re.M)
        if len(ids)!=1 or ids[0] not in byrun:
            issues.append("Run ID missing: "+str(log));continue
        d=byrun[ids[0]]
        td=trial.get("telemetry_dir","")
        telemetry=readcsv(pathlib.Path(td)/"kubelet_summary_samples.csv") if td else []
        cpu=[float(r["matching_container_cpu_nanocores"])/1e9 for r in telemetry
             if r.get("matching_container_cpu_nanocores") not in ("",None)]
        mem=[float(r["matching_container_mem_working_set_bytes"])/1048576 for r in telemetry
             if r.get("matching_container_mem_working_set_bytes") not in ("",None)]
        data.append({
            "stage":trial["stage"],"trial":int(trial["trial"]),"seed":int(trial["seed"]),
            "mode":trial["mode"],"run_id":ids[0],"requests":int(trial["requests"]),
            "succeeded":d["successes"],
            "FGP_ms":d["FGP_placement_ms"],"DCO_ms":d["DCO_ordering_ms"],
            "ILRSA_placement_ms":readjson(suite/"runs"/ids[0]/"summary.json").get("ilrsa_placement_ms"),
            "ILRSA_ordering_ms":readjson(suite/"runs"/ids[0]/"summary.json").get("ilrsa_ordering_ms"),
            "controller_ms":d["FGP_DCO_combined_controller_ms"],
            "api_ack_mean_ms":d["api_ack_mean_ms"],
            "create_to_start_mean_s":d["create_to_start_mean_s"],
            "create_to_start_p95_s":d["create_to_start_P95_s"],
            "pulling_events":d["pulling_events"],
            "already_present_events":d["already_present_events"],
            "successful_pull_events":d["successfully_pulled_events"],
            "pull_mean_s":d["observed_pull_duration_mean_s"],
            "ACT_s":d["ACT_all_real_s"],"P95_s":d["P95_all_real_s"],
            "D_s":d["D_real_s"],"batch_wall_s":d["batch_wall_s"],
            "frag_static_NOT_LIVE":d["Frag_static_placement_projection"],
            "exp_cpu_peak_cores":max(cpu) if cpu else None,
            "exp_mem_peak_MiB":max(mem) if mem else None,
            "node_counts":d["node_counts"]
        })
    writecsv(suite/"stage_breakdown_by_trial.csv",data)
    groups=defaultdict(list)
    for x in data:groups[(x["stage"],x["mode"])].append(x)
    columns=("FGP_ms","DCO_ms","ILRSA_placement_ms","ILRSA_ordering_ms",
        "controller_ms","api_ack_mean_ms",
        "create_to_start_mean_s","pull_mean_s","ACT_s","P95_s","D_s",
        "batch_wall_s","frag_static_NOT_LIVE","exp_cpu_peak_cores","exp_mem_peak_MiB")
    summary=[]
    for (stage,mode),group in sorted(groups.items()):
        r={"stage":stage,"mode":mode,"trials":len(group),
           "successful_trials":sum(x["requests"]==x["succeeded"] for x in group),
           "pulling_events_total":sum(x["pulling_events"] for x in group),
           "already_present_events_total":sum(x["already_present_events"] for x in group)}
        for col in columns:r[col+"_mean"]=avg(x[col] for x in group)
        summary.append(r)
    writecsv(suite/"stage_breakdown_summary.csv",summary)
    lines=[
        "# 官方 Docker Hub 镜像压力实验：阶段性测量","",
        "数值取有效重复的均值；破折号表示不存在该阶段或无有效采样。","",
        "| 实验 | 方法 | 重复成功 | FGP ms | DCO ms | ILR-SA放置 ms | ILR-SA排序 ms | API ms | Pod启动 s | Pulling | ACT s | P95 s | D s | 实际CPU峰值 core | 实际内存峰值 MiB |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    ]
    for x in summary:
        lines.append(
            f"| {x['stage']} | {x['mode']} | {x['successful_trials']}/{x['trials']} | "
            f"{display(x['FGP_ms_mean'])} | {display(x['DCO_ms_mean'])} | "
            f"{display(x['ILRSA_placement_ms_mean'])} | {display(x['ILRSA_ordering_ms_mean'])} | "
            f"{display(x['api_ack_mean_ms_mean'])} | "
            f"{display(x['create_to_start_mean_s_mean'])} | {x['pulling_events_total']} | "
            f"{display(x['ACT_s_mean'])} | {display(x['P95_s_mean'])} | "
            f"{display(x['D_s_mean'])} | {display(x['exp_cpu_peak_cores_mean'])} | "
            f"{display(x['exp_mem_peak_MiB_mean'])} |"
        )
    lines += [
        "","## 核心限制","",
        "- FGP / DCO 为控制器内规划耗时，API 为提交确认，不是容器部署时间。",
        "- Pod 创建到启动时间混合调度、拉取和容器启动，不能当成纯镜像传输耗时。",
        "- Pulling/Pulled 来自 Kubernetes Event，不是下载流量字节数。",
        "- 真实 CPU/内存为 kubelet 定时采样峰值，不能以 requests 代替。",
        "- Frag 目前仅是全量 requests 静态投影，不是运行时动态碎片化。",
        "- 未实现物理 PGDSF 驱逐及真实缓存配额，不可声称在实机验证缓存替换优势。",
        "- FALCONS/FGP+FCFS 使用外部 nodeName 定向；K8s Default 使用原生调度器。",
        "- 全量并发场景 DCO 顺序可能不产生效果，需与低并发顺序场景分开看。",
        "","## 审计异常",""
    ]
    lines.extend("- "+x for x in issues)
    if not issues:lines.append("- 无输入关联或执行记录异常。")
    (suite/"stage_breakdown_report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print("Saved",suite/"stage_breakdown_report.md")
    print("Included",len(data),"runs; issues",len(issues))
    for x in issues:print("WARN",x,file=sys.stderr)
    return 0 if not issues else 2

if __name__=="__main__":
    sys.exit(main())
