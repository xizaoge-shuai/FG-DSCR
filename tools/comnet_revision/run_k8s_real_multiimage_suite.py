#!/usr/bin/env python3
"""Paired small REAL OCI image-diversity trial launcher (exploratory).

Stages:
  pilot: N=10, image catalog=12; one or more matched trials.
  catalog: N=20, image catalog=4/8/12; paired methods with same seed.
  requests: N=10/20/40, image catalog=12; paired methods with same seed.

Methods run in reproducibly randomized order on a previously confirmed warm
cache. Fixed global inflight limit of 3 is not a fully equivalent Kubernetes
scheduling treatment. Do not interpret observed differences as PGDSF evidence.
Logs and manifests retained. No namespace/image cleanup.
"""
import argparse,csv,datetime as dt,json,os,pathlib,random,signal,subprocess,sys,time,uuid

MODES=("falcons","fgp_fcfs","default")
def log_run(cmd,log_path,env):
    with log_path.open("w",encoding="utf-8") as f:
        p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                           text=True,bufsize=1,env=env)
        try:
            for line in p.stdout:
                sys.stdout.write(line);sys.stdout.flush()
                f.write(line);f.flush()
            return p.wait()
        except KeyboardInterrupt:
            p.terminate()
            try:p.wait(timeout=10)
            except subprocess.TimeoutExpired:p.kill()
            raise

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--stage",required=True,choices=["pilot","catalog","requests"])
    p.add_argument("--catalog",default="results/comnet_revision/k8s_real_testbed/oci_v2_12/catalog.json")
    p.add_argument("--repeats",type=int,default=1)
    p.add_argument("--base-seed",type=int,default=20261018)
    p.add_argument("--max-inflight",type=int,default=3)
    p.add_argument("--timeout",type=int,default=900)
    p.add_argument("--modes",nargs="+",choices=list(MODES),default=list(MODES))
    p.add_argument("--with-telemetry",action="store_true",help="Sample kubelet node/Pod CPU/memory/network for each policy run")
    p.add_argument("--out-dir",default="results/comnet_revision/k8s_real_testbed/multiimage_suite")
    p.add_argument("--resume-dir",default="",help="Resume an interrupted existing suite, skipping only verified completed runs")
    a=p.parse_args()
    if not 1<=a.repeats<=20:p.error("repeats must be 1..20")
    catalog=json.loads(pathlib.Path(a.catalog).read_text(encoding="utf-8"))
    if catalog.get("errors") or len(catalog.get("images",[]))<12:
        p.error("Need a verified 12-image OCI catalog with zero errors")
    if a.stage=="pilot":
        scenarios=[(10,12)]
    elif a.stage=="catalog":
        scenarios=[(20,4),(20,8),(20,12)]
    else:
        scenarios=[(10,12),(20,12),(40,12)]
    job_script=pathlib.Path(__file__).with_name("k8s_real_oci_smoke.py")
    telemetry_script=pathlib.Path(__file__).with_name("k8s_real_kubelet_sampler.py")
    resuming=bool(a.resume_dir)
    out=(pathlib.Path(a.resume_dir) if resuming else
         pathlib.Path(a.out_dir)/("suite-"+dt.datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6]))
    if resuming:
        if not out.is_dir():p.error("Resume suite directory does not exist: "+str(out))
    else:
        out.mkdir(parents=True,exist_ok=False)
    run_artifacts=out/"runs"
    if resuming:
        if not run_artifacts.is_dir():p.error("Missing historical runs directory: "+str(run_artifacts))
    else:
        run_artifacts.mkdir()
    manifest=out/"trials.csv"
    fields=["stage","requests","image_count","trial","seed","mode",
            "mode_order","start_utc","wall_s","exit_code","log_path","telemetry_dir","profile",
            "image_selection","task_profile"]
    env=dict(os.environ);env["PYTHONHASHSEED"]="2"
    configfile=out/"suite_config.json"
    config={
        "stage":a.stage,"scenarios":scenarios,"repeats":a.repeats,
        "seed_base":a.base_seed,"modes":a.modes,"cache_model":"warm_all_verified",
        "image_selection":"zipf_like","zipf_alpha":1.1,"task_profile":"mixed",
        "max_inflight":a.max_inflight,"catalog":a.catalog,
        "run_artifacts":str(run_artifacts),
        "with_telemetry":a.with_telemetry,
        "limitation":"All 12 images must be verified cached on all 3 workers before running; no automatic cache reset/physical PGDSF."
    }
    completed=set()
    if resuming:
        if not configfile.is_file() or not manifest.is_file():
            p.error("Resume requires both suite_config.json and trials.csv")
        old=json.loads(configfile.read_text(encoding="utf-8"))
        for k in ("stage","scenarios","repeats","seed_base","modes","cache_model",
                  "image_selection","zipf_alpha","task_profile","max_inflight",
                  "catalog","run_artifacts","with_telemetry"):
            if (list(map(tuple,old.get(k,[])))!=list(map(tuple,config.get(k,[]))) if k=="scenarios" else old.get(k)!=config.get(k)):
                p.error(f"Resume configuration mismatch {k}: old={old.get(k)!r} current={config.get(k)!r}. "
                        "Use the EXACT options from the original suite.")
        with manifest.open(newline="",encoding="utf-8") as saved:
            reader=csv.DictReader(saved)
            if reader.fieldnames!=fields:
                p.error("Resume trials.csv schema differs from this runner")
            for row in reader:
                if row["exit_code"]!="0":
                    p.error("trials.csv contains a non-successful recorded row; "
                            "manually inspect/reconcile the failed attempt before resume")
                key=(int(row["requests"]),int(row["image_count"]),int(row["trial"]),
                     int(row["seed"]),row["mode"])
                if key in completed:p.error("Duplicate successful trial "+str(key))
                log=pathlib.Path(row["log_path"])
                if not log.is_file():p.error("Missing completed run log: "+str(log))
                import re
                ids=re.findall(r"^RUN (oci-[a-f0-9]+)\b",log.read_text(encoding="utf-8",errors="replace"),re.M)
                if len(ids)!=1:p.error("Cannot verify exactly one completed RUN in "+str(log))
                summaryfile=run_artifacts/ids[0]/"summary.json"
                if not summaryfile.is_file():p.error("Missing completed summary: "+str(summaryfile))
                run_summary=json.loads(summaryfile.read_text(encoding="utf-8"))
                if run_summary.get("failure") or run_summary.get("succeeded")!=int(row["requests"]) or run_summary.get("mode")!=row["mode"]:
                    p.error("Recorded successful run failed evidence validation: "+str(summaryfile))
                completed.add(key)
        print(f"[RESUME] {out}: validated {len(completed)} completed policy runs; "
              "remaining policies will run in original seeded order. "
              "Check no old workload process is still running.",flush=True)
    else:
        configfile.write_text(json.dumps(config,indent=2,ensure_ascii=False),encoding="utf-8")
    with manifest.open("a" if resuming else "w",encoding="utf-8",newline="") as f:
        wr=csv.DictWriter(f,fieldnames=fields)
        if not resuming:
            wr.writeheader();f.flush()
        for n,imcount in scenarios:
            for trial in range(a.repeats):
                seed=a.base_seed+trial
                modes=list(a.modes)
                random.Random(113*n+307*imcount+997*trial).shuffle(modes)
                for index,mode in enumerate(modes,1):
                    key=(n,imcount,trial+1,seed,mode)
                    if key in completed:
                        print(f"[RESUME-SKIP] N={n} images={imcount} trial={trial+1} seed={seed} mode={mode}",flush=True)
                        continue
                    record={"stage":a.stage,"requests":n,"image_count":imcount,
                            "trial":trial+1,"seed":seed,"mode":mode,
                            "mode_order":index,"start_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
                            "profile":"paper_declared","image_selection":"zipf_like",
                            "task_profile":"mixed"}
                    log_path=(out/f"n{n}_img{imcount}_trial{trial+1:02d}_{mode}.log")
                    if resuming and log_path.exists():
                        # An interrupted unrecorded attempt is NOT a completed trial.
                        # Preserve its log for audit and use a new log file.
                        log_path=out/f"n{n}_img{imcount}_trial{trial+1:02d}_{mode}_retry_{uuid.uuid4().hex[:6]}.log"
                    cmd=[sys.executable,"-u",str(job_script),
                         "--catalog",a.catalog,"--mode",mode,
                         "--count",str(n),"--image-count",str(imcount),
                         "--image-selection","zipf_like","--zipf-alpha","1.1",
                         "--task-profile","mixed","--seed",str(seed),
                         "--max-inflight",str(a.max_inflight),
                         "--profile","paper_declared",
                         "--cache-model","warm_all_verified",
                         "--timeout",str(a.timeout),
                         "--out-root",str(run_artifacts)]
                    print(f"\n===== {a.stage} N={n} images={imcount} "
                          f"trial={trial+1}/{a.repeats} seed={seed} "
                          f"mode={mode} position={index} =====",flush=True)
                    t0=time.monotonic()
                    sampler=None;sampler_handle=None;telemetry_dir=None
                    if a.with_telemetry:
                        telemetry_dir=out/f"telemetry_n{n}_img{imcount}_trial{trial+1:02d}_{mode}"
                        if resuming and telemetry_dir.exists():
                            telemetry_dir=out/f"telemetry_n{n}_img{imcount}_trial{trial+1:02d}_{mode}_retry_{uuid.uuid4().hex[:6]}"
                        telemetry_dir.mkdir()
                        sampler_handle=(telemetry_dir/"sampler.log").open("w",encoding="utf-8")
                        sampler=subprocess.Popen([
                            sys.executable,"-u",str(telemetry_script),
                            "--interval","3","--max-duration",str(a.timeout+90),
                            "--out-dir",str(telemetry_dir),
                        ],stdout=sampler_handle,stderr=subprocess.STDOUT,env=env)
                        time.sleep(1.5)
                    try:
                        code=log_run(cmd,log_path,env)
                    finally:
                        if sampler is not None:
                            if sampler.poll() is None:
                                sampler.send_signal(signal.SIGINT)
                            try:sampler.wait(timeout=30)
                            except subprocess.TimeoutExpired:
                                sampler.terminate()
                                sampler.wait(timeout=10)
                            sampler_handle.close()
                    record.update(wall_s=round(time.monotonic()-t0,3),
                                  exit_code=code,log_path=str(log_path),
                                  telemetry_dir=str(telemetry_dir) if telemetry_dir else "")
                    wr.writerow(record);f.flush()
                    if code:
                        print(f"FAILED code={code}. Stop preserving K8s namespace and raw evidence. "
                              f"See {log_path}",file=sys.stderr,flush=True)
                        return code
    print(f"SUITE COMPLETE manifest={manifest}",flush=True)
    return 0
if __name__=="__main__":sys.exit(main())
