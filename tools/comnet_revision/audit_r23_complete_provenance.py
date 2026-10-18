#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only COMNET reviewer R2.3 reproducibility audit.

Outputs a provenance markdown report, request-to-image mapping, image rank
weights, original file inventory, and quality warnings. No experiment or
original scientific input is modified.
"""
import argparse,csv,hashlib,json,math,statistics,subprocess
from pathlib import Path
from collections import Counter,defaultdict

def read(p):
    return json.loads(p.read_text(encoding="utf-8"))
def sha(p):
    h=hashlib.sha256()
    with p.open("rb") as f:
        for c in iter(lambda:f.read(1048576*4),b""):
            h.update(c)
    return h.hexdigest()
def output(path,rows,cols):
    with path.open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=cols,extrasaction="ignore")
        w.writeheader();w.writerows(rows)
def sig(seq):
    return tuple(sorted(set(map(str,seq))))
def imsize(im):
    t=0
    for layer in im["layers"]:
        mb=int(round(float(layer["size_mb"])))
        if mb<=0:mb=max(1,int(math.ceil(float(layer["size_bytes"])/1048576)))
        t+=mb
    return t
def mdrows(p):
    cols=None
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.startswith("|"):continue
        cells=[c.strip() for c in ln.strip().strip("|").split("|")]
        if {"Delay_all","CA_rate","frag_score"}.issubset(cells):cols=cells
        elif cols and len(cells)==len(cols) and cells[0].isdigit():
            yield dict(zip(cols,cells))
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--repo",type=Path,default=Path.cwd())
    ap.add_argument("--out-dir",type=Path,default=Path("results/comnet_revision/reproducibility_r23/detailed"))
    args=ap.parse_args()
    root=args.repo.expanduser().resolve()
    dest=args.out_dir if args.out_dir.is_absolute() else root/args.out_dir
    dest.mkdir(parents=True,exist_ok=True)
    try:commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True,timeout=15).strip()
    except Exception:commit="unknown"
    log=["# R2.3 历史实验可复现性核查","",
         "说明：只检查已有文件，不会重生成或覆盖输入数据。",
         "代码版本："+commit,""]
    warnings=[]
    def note(x=""):log.append(x)
    raw=root/"data/raw/drtp_raw/data_centers"
    note("## 一、DRTP 原始数据与统计")
    if raw.is_dir():
        files=sorted(f for f in raw.rglob("*") if f.is_file())
        counts=Counter(f.relative_to(raw).parts[0] for f in files)
        inventory=[{"path":f.relative_to(root).as_posix(),"bytes":f.stat().st_size} for f in files]
        output(dest/"raw_trace_inventory.csv",inventory,["path","bytes"])
        note(f"- 原始文件 {len(files)} 个，体积 {sum(x['bytes'] for x in inventory)/1073741824:.3f} GiB。")
        note("- 目录分布："+", ".join(f"{x}:{n}" for x,n in sorted(counts.items())))
        note("- 原始文件路径/体积清单：raw_trace_inventory.csv（不等同于内容哈希清单）。")
    else:
        warnings.append("raw directory missing");note("- 原始轨迹目录缺失。")
    parser=root/"scripts/drtp_collect_and_stats.py"
    if parser.exists():note("- 解析器 scripts/drtp_collect_and_stats.py，SHA-256："+sha(parser))
    prof=root/"data/stats/drtp_stats/drtp_profile.json"
    popularity=root/"data/stats/drtp_stats/repo_popularity.csv"
    profile={}
    if prof.exists():
        profile=read(prof);w=profile.get("repo_weights",[])
        note(f"- drtp_profile.json：total_requests={profile.get('total_requests')}，repo_weights={len(w)}，SHA-256={sha(prof)}。")
        if w:
            note(f"- 前 {len(w)} 权重总和={sum(float(z['weight']) for z in w):.9f}。")
        if popularity.exists():
            with popularity.open(newline="",encoding="utf-8-sig") as f:
                same=list(csv.DictReader(f))[:len(w)]
            total=float(profile.get("total_requests") or 0)
            match=total>0 and len(same)==len(w) and all(
                str(a["repo_id"])==str(b["repo_id"]) and
                abs(float(a["weight"])-int(b["requests"])/total)<1e-10
                for a,b in zip(w,same))
            note(f"- 与 repo_popularity.csv 前 {len(w)} 行逐项一致：{match}。")
            if not match:warnings.append("repo weights mismatch")
        note("- DRTP 是 HTTP Registry 访问轨迹，包含 manifests/blobs 等请求，并非逐个容器启动事件；主实验仅使用仓库热度。")
    else:warnings.append("profile missing")
    note("")
    note("## 二、Docker Hub 镜像及匿名仓库名次映射")
    catpath=root/"data/catalog/image_catalog_large_v2.json"
    cats=read(catpath) if catpath.exists() else []
    index=defaultdict(list)
    if cats:
        for im in cats:index[sig(x["digest"] for x in im.get("layers",[]))].append(im["image_ref"])
        note(f"- 候选镜像数={len(cats)}，不同真实层摘要数={len(set(x['digest'] for im in cats for x in im['layers']))}。")
        note("- 镜像目录 SHA-256："+sha(catpath))
    else:warnings.append("catalog missing")
    if cats and profile.get("repo_weights"):
        ranks=sorted(cats,key=imsize,reverse=True)
        raww=[float(profile["repo_weights"][min(i,len(profile["repo_weights"])-1)]["weight"]) for i in range(len(ranks))]
        total=sum(raww)
        maps=[{"image_rank":i+1,"image_ref":im["image_ref"],"image_mb":imsize(im),
               "anonymous_popularity_rank":i+1,"source_weight":raww[i],
               "mapped_probability":raww[i]/total if total else 1/len(ranks)}
              for i,im in enumerate(ranks)]
        output(dest/"rank_mapping.csv",maps,list(maps[0]))
        note("- rank_mapping.csv：匿名仓库访问热度名次与按层大小降序排列的 Docker Hub 镜像名次对应；映射概率重新归一化。")
        note("- 这不意味着匿名仓库真实身份已知，也不是按请求时间戳逐条重放。")
    note("")
    note("## 三、实际请求与随机种子")
    case=root/"cases/drtp_cache_only_sweep_88_prefix/drtp_img88_cacheonly_1024mb_1000.json"
    req=[]
    if case.exists():
        obj=read(case);containers=obj["containers"]
        freq=Counter();valid=missing=ambiguous=0
        for c in containers:
            s=sig(c["layers"]);found=index.get(s,[])
            image=found[0] if len(found)==1 else ("UNMATCHED" if not found else "AMBIGUOUS")
            valid+=len(found)==1;missing+=not found;ambiguous+=len(found)>1
            freq[image]+=1
            req.append({"cid":c["cid"],"image_ref":image,
                        "candidates":";".join(found),"service_type":c.get("service_type",""),
                        "cpu":c["resources"].get("cpu"),"mem":c["resources"].get("mem"),
                        "disk":c["resources"].get("disk"),"run_time_s":c.get("run_time"),
                        "layer_signature_sha256":hashlib.sha256("\n".join(s).encode()).hexdigest()})
        output(dest/"request_to_image.csv",req,list(req[0]))
        ranked=sorted(freq.items(),key=lambda x:(-x[1],x[0]))
        output(dest/"request_image_popularity.csv",
               [{"image_ref":im,"requests":k,"ratio":k/len(req)} for im,k in ranked],
               ["image_ref","requests","ratio"])
        note(f"- 主实验请求数 {len(req)}，候选镜像 88，出现层组合 {len(set(sig(c['layers']) for c in containers))}。")
        note(f"- 输入元数据 seed={obj.get('meta',{}).get('seed')}；SHA-256={sha(case)}。")
        note(f"- 镜像唯一反查 {valid}，不匹配 {missing}，有歧义 {ambiguous}。")
        note(f"- top1={ranked[0][1]/len(req):.4%}，top5={sum(k for _,k in ranked[:5])/len(req):.4%}。")
        if missing or ambiguous:warnings.append("request/image mismatch")
        prefixdir=case.parent;checked=0;errors=[]
        for f in sorted(prefixdir.glob("drtp_img88_cacheonly_1024mb_*.json")):
            if f==case:continue
            checked+=1
            try:
                q=read(f)["containers"]
                if q!=containers[:len(q)]:errors.append(f.name)
            except Exception:errors.append(f.name)
        note(f"- 与 1000 请求逐项前缀比对 {checked} 个文件，异常 {len(errors)}。")
        if errors:
            note("- 不一致文件："+",".join(errors[:10]));warnings.append("prefix mismatch")
    else:warnings.append("1000 request case missing")
    gen=root/"scripts/build_fg_case_from_catalog_and_trace.py"
    if gen.exists():
        note("- 原始生成器 SHA-256："+sha(gen))
        note("- 镜像按 DRTP 热度加权采样；资源/服务时间取决于镜像类别的离散随机选项，不是 DRTP 直接测得的时长。")
    note("- seed 元数据仅证明文件记录了该值，尚不能证明历史重生成时实际使用相同命令及文件版本。")
    note("")
    note("## 四、J 归一化的原始来源")
    detail=root/"results/drtp/k8s_same_scale/tables_allrequest_fig5_edgeca300_prefix/fig5_allrequest_detail.md"
    if detail.exists():
        rows=list(mdrows(detail))
        if rows:
            triples=[(float(r["Delay_all"]),float(r["CA_rate"]),float(r["frag_score"])) for r in rows]
            means=[statistics.mean(z[i] for z in triples) for i in range(3)]
            note(f"- 原始评价记录 {len(rows)} 条；SHA-256={sha(detail)}。")
            note(f"- D_ref={means[0]:.15g}，CA_ref={means[1]:.15g}，Frag_ref={means[2]:.15g}。")
            note("- J=D/D_ref+CA_rate/CA_ref+Frag/Frag_ref。")
            note("- 参考值由主评价集合计算，不是独立留出校准集；后来实验应固定上述值。")
            if len(rows)!=54:warnings.append("not 54 rows")
        else:warnings.append("no evaluation rows")
    else:warnings.append("evaluation detail missing")
    note("")
    note("## 五、历史参数")
    params=root/"results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env"
    if params.exists():
        note("- 历史参数文件 SHA-256："+sha(params))
        note(params.read_text(encoding="utf-8").strip())
    else:warnings.append("selected parameter config missing")
    note("- FGP 的势函数分项在代码中按当前候选放置动态缩放，不是固定的预先校准尺度。")
    note("- 尚需核对原稿表格、Fig.8 历史运行参数及实际重现命令。")
    (dest/"provenance_report.md").write_text("\n".join(log)+"\n",encoding="utf-8")
    (dest/"quality_warnings.json").write_text(json.dumps(warnings,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("Saved",dest/"provenance_report.md")
    print("Image unique matched",sum(x["image_ref"] not in ("UNMATCHED","AMBIGUOUS") for x in req),
          "/",len(req))
    print("Warnings:",warnings)
if __name__=="__main__":
    main()
