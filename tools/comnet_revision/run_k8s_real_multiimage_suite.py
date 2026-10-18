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
import argparse,csv,datetime as dt,json,os,pathlib,random,subprocess,sys,time,uuid

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
    p.add_argument("--out-dir",default="results/comnet_revision/k8s_real_testbed/multiimage_suite")
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
    out=pathlib.Path(a.out_dir)/("suite-"+dt.datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6])
    out.mkdir(parents=True,exist_ok=False)
    manifest=out/"trials.csv"
    fields=["stage","requests","image_count","trial","seed","mode",
            "mode_order","start_utc","wall_s","exit_code","log_path","profile",
            "image_selection","task_profile"]
    env=dict(os.environ);env["PYTHONHASHSEED"]="2"
    (out/"suite_config.json").write_text(json.dumps({
        "stage":a.stage,"scenarios":scenarios,"repeats":a.repeats,
        "seed_base":a.base_seed,"modes":a.modes,"cache_model":"warm_all_verified",
        "image_selection":"zipf_like","zipf_alpha":1.1,"task_profile":"mixed",
        "max_inflight":a.max_inflight,"catalog":a.catalog,
        "limitation":"All 12 images must be verified cached on all 3 workers before running; no automatic cache reset/physical PGDSF."
    },indent=2,ensure_ascii=False),encoding="utf-8")
    with manifest.open("w",encoding="utf-8",newline="") as f:
        wr=csv.DictWriter(f,fieldnames=fields);wr.writeheader();f.flush()
        for n,imcount in scenarios:
            for trial in range(a.repeats):
                seed=a.base_seed+trial
                modes=list(a.modes)
                random.Random(113*n+307*imcount+997*trial).shuffle(modes)
                for index,mode in enumerate(modes,1):
                    record={"stage":a.stage,"requests":n,"image_count":imcount,
                            "trial":trial+1,"seed":seed,"mode":mode,
                            "mode_order":index,"start_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
                            "profile":"paper_declared","image_selection":"zipf_like",
                            "task_profile":"mixed"}
                    log_path=(out/f"n{n}_img{imcount}_trial{trial+1:02d}_{mode}.log")
                    cmd=[sys.executable,"-u",str(job_script),
                         "--catalog",a.catalog,"--mode",mode,
                         "--count",str(n),"--image-count",str(imcount),
                         "--image-selection","zipf_like","--zipf-alpha","1.1",
                         "--task-profile","mixed","--seed",str(seed),
                         "--max-inflight",str(a.max_inflight),
                         "--profile","paper_declared",
                         "--cache-model","warm_all_verified",
                         "--timeout",str(a.timeout)]
                    print(f"\n===== {a.stage} N={n} images={imcount} "
                          f"trial={trial+1}/{a.repeats} seed={seed} "
                          f"mode={mode} position={index} =====",flush=True)
                    t0=time.monotonic()
                    code=log_run(cmd,log_path,env)
                    record.update(wall_s=round(time.monotonic()-t0,3),
                                  exit_code=code,log_path=str(log_path))
                    wr.writerow(record);f.flush()
                    if code:
                        print(f"FAILED code={code}. Stop preserving K8s namespace and raw evidence. "
                              f"See {log_path}",file=sys.stderr,flush=True)
                        return code
    print(f"SUITE COMPLETE manifest={manifest}",flush=True)
    return 0
if __name__=="__main__":sys.exit(main())
