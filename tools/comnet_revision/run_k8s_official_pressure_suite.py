#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paired official Docker Hub OCI pressure trials, preserving complete logs.

Stage placement: N=12, 12 concurrent CPU-active Pods, tests FGP and K8s.
Stage ordering: N=12, 3 inflight CPU-active Pods, tests FGP+DCO vs FGP+FCFS.
Stage pilot: N=6, 3 inflight; first verify busybox/sh/dd compatibility.
Physical caches are uncontrolled; no physical PGDSF eviction.
"""
import argparse,csv,datetime as dt,hashlib,json,os,pathlib,random,re,signal,subprocess,sys,time,uuid

METHODS=("falcons","fgp_fcfs","default")
COLUMNS=["stage","requests","image_count","trial","seed","mode","mode_order",
         "start_utc","wall_s","exit_code","log_path","telemetry_dir","profile",
         "image_selection","task_profile"]

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def execute(argv,log,env):
    with log.open("w",encoding="utf-8") as f:
        child=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                               text=True,bufsize=1,env=env)
        try:
            for ln in child.stdout:
                print(ln,end="",flush=True)
                f.write(ln);f.flush()
            return child.wait()
        except KeyboardInterrupt:
            child.terminate()
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:child.kill()
            raise

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--stage",choices=("pilot","placement","ordering"),required=True)
    ap.add_argument("--catalog",default="results/comnet_revision/k8s_real_testbed/oci_official_12/catalog.json")
    ap.add_argument("--root",default="results/comnet_revision/k8s_real_testbed/official_pressure_suite")
    ap.add_argument("--repeats",type=int,default=3)
    ap.add_argument("--seed-base",type=int,default=20261020)
    ap.add_argument("--timeout",type=int,default=1200)
    ap.add_argument("--with-telemetry",action="store_true")
    ap.add_argument("--resume-dir",default="")
    a=ap.parse_args()
    if not 1<=a.repeats<=10:ap.error("repeats 1..10")
    catalogpath=pathlib.Path(a.catalog)
    cat=json.loads(catalogpath.read_text(encoding="utf-8"))
    images=cat.get("images",[])
    if cat.get("errors") or len(images)!=12:ap.error("Need exactly 12 verified OCI images")
    if any(not x["image_ref"].startswith("docker.io/library/") for x in images):
        ap.error("Catalog contains non-official Docker Hub images")
    if any("@sha256:" not in x["image_ref"] for x in images):
        ap.error("Catalog has mutable unpinned tags")
    unique={}
    for im in images:
        for blob in im["layers"]:
            d=blob["digest"];b=int(blob["size_bytes"])
            if d in unique and unique[d]!=b:ap.error("Conflicting layer sizes")
            unique[d]=b
    unique_mib=sum(unique.values())/1048576
    print(f"[CATALOG] official={len(images)} unique_layers={len(unique)} "
          f"unique_compressed_MiB={unique_mib:.2f} digest_pinned=yes",flush=True)
    n=6 if a.stage=="pilot" else 12
    max_inflight=12 if a.stage=="placement" else 3
    out=pathlib.Path(a.resume_dir) if a.resume_dir else (
        pathlib.Path(a.root)/("suite-"+dt.datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6]))
    if a.resume_dir:
        if not out.is_dir():ap.error("Resume path does not exist")
    else:out.mkdir(parents=True,exist_ok=False)
    (out/"runs").mkdir(exist_ok=True)
    config={"stage":a.stage,"n":n,"inflight":max_inflight,"repeats":a.repeats,
            "seed_base":a.seed_base,"catalog":str(catalogpath),"catalog_sha256":sha(catalogpath),
            "image_count":12,"task_profile":"pressure","profile":"paper_declared",
            "cache_model":"empty_assumed","image_selection":"round_robin","with_telemetry":a.with_telemetry,"timeout":a.timeout}
    cfgfile=out/"suite_config.json";manifest=out/"trials.csv"
    done=set()
    if a.resume_dir:
        if not cfgfile.exists() or not manifest.exists():ap.error("Resume needs config and trials")
        if json.loads(cfgfile.read_text(encoding="utf-8"))!=config:ap.error("Resume config mismatch")
        with manifest.open(newline="",encoding="utf-8") as f:
            reader=csv.DictReader(f)
            if reader.fieldnames!=COLUMNS:ap.error("CSV format mismatch")
            for x in reader:
                if x["exit_code"]!="0":ap.error("Existing failed trial; inspect manually")
                log=pathlib.Path(x["log_path"])
                if not log.exists():ap.error("Missing successful trial log")
                ids=re.findall(r"^RUN (oci-[a-f0-9]+)\b",log.read_text(encoding="utf-8",errors="replace"),re.M)
                if len(ids)!=1:ap.error("Cannot confirm trial run ID")
                proof=out/"runs"/ids[0]/"summary.json"
                if not proof.exists():ap.error("Missing trial summary")
                result=json.loads(proof.read_text(encoding="utf-8"))
                if result.get("mode")!=x["mode"] or result.get("succeeded")!=n or result.get("failure"):
                    ap.error("Completed trial evidence invalid")
                done.add((int(x["trial"]),x["mode"]))
        print("[RESUME] already complete:",len(done),flush=True)
    else:cfgfile.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding="utf-8")
    adapter=pathlib.Path(__file__).with_name("k8s_real_oci_smoke.py")
    sampler=pathlib.Path(__file__).with_name("k8s_real_kubelet_sampler.py")
    env=os.environ.copy();env["PYTHONHASHSEED"]="2"
    with manifest.open("a" if a.resume_dir else "w",encoding="utf-8",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=COLUMNS)
        if not a.resume_dir:writer.writeheader();f.flush()
        for trial in range(1,a.repeats+1):
            seed=a.seed_base+trial-1
            modes=list(METHODS)
            random.Random(113*n+997*trial).shuffle(modes)
            for order,mode in enumerate(modes,1):
                if (trial,mode) in done:continue
                logfile=out/f"trial{trial:02d}_{mode}.log"
                if logfile.exists():logfile=out/f"trial{trial:02d}_{mode}_retry_{uuid.uuid4().hex[:6]}.log"
                telemetry=out/f"telemetry_trial{trial:02d}_{mode}"
                if telemetry.exists():telemetry=out/f"telemetry_trial{trial:02d}_{mode}_retry_{uuid.uuid4().hex[:6]}"
                cmd=[sys.executable,"-u",str(adapter),"--catalog",str(catalogpath),
                     "--mode",mode,"--count",str(n),"--image-count","12",
                     "--image-selection","round_robin",
                     "--task-profile","pressure","--seed",str(seed),
                     "--max-inflight",str(max_inflight),"--profile","paper_declared",
                     "--cache-model","empty_assumed","--timeout",str(a.timeout),
                     "--out-root",str(out/"runs")]
                print(f"\n[RUN] {a.stage}: N={n}, inflight={max_inflight}, "
                      f"trial={trial}/{a.repeats}, seed={seed}, mode={mode}",flush=True)
                process=None;handle=None
                if a.with_telemetry:
                    telemetry.mkdir()
                    handle=(telemetry/"sampler.log").open("w",encoding="utf-8")
                    process=subprocess.Popen([sys.executable,"-u",str(sampler),
                            "--interval","3","--max-duration",str(a.timeout+90),
                            "--out-dir",str(telemetry)],
                            stdout=handle,stderr=subprocess.STDOUT,env=env)
                    time.sleep(1.5)
                t=time.monotonic()
                try:code=execute(cmd,logfile,env)
                finally:
                    if process is not None:
                        if process.poll() is None:process.send_signal(signal.SIGINT)
                        try:process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            process.terminate();process.wait(timeout=12)
                        handle.close()
                row={"stage":a.stage,"requests":n,"image_count":12,"trial":trial,
                     "seed":seed,"mode":mode,"mode_order":order,
                     "start_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
                     "wall_s":round(time.monotonic()-t,3),"exit_code":code,
                     "log_path":str(logfile),
                     "telemetry_dir":str(telemetry) if a.with_telemetry else "",
                     "profile":"paper_declared","image_selection":"round_robin",
                     "task_profile":"pressure"}
                writer.writerow(row);f.flush()
                if code:return code
    print("SUITE COMPLETE:",out)
    print("Analysis command: python3 tools/comnet_revision/analyze_k8s_real_suites.py "
          f"--suite-dir {out} --catalog {catalogpath} --out-dir {out/'analysis'}")
    return 0
if __name__=="__main__":sys.exit(main())
