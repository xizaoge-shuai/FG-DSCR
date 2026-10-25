#!/usr/bin/env python3
"""Measure real-node OCI transfer, unpack time, and one-layer physical eviction.

Safety: inventory/unpack are non-destructive. The contention mode deletes one
verified, catalog-unique blob from containerd on participating workers and
requires --apply-eviction. It refuses active Pods using the selected image.
Host RX/TX is only contextual; tcpdump on the containerd proxy path records
TCP payload bytes, while the catalog gives exact compressed blob bytes.
"""
import argparse, csv, datetime as dt, json, pathlib, re, shlex, subprocess
import sys, time, uuid
from collections import Counter

DEFAULT_WORKERS = ["falcons-worker1", "falcons-worker2",
                   "falcons-worker3", "falcons-worker4"]
SHELL_IMAGES = ("alpine", "busybox", "debian", "ubuntu", "python", "node", "golang")

def call(cmd, timeout=60, check=True):
    p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError("$ " + " ".join(cmd) + "\n" + p.stderr[-1800:])
    return p

def kubectl(*args, timeout=90):
    return call(["kubectl", "--request-timeout=30s", *args], timeout=timeout).stdout

def jsonwrite(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def csvwrite(path, rows):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def get_nodes():
    obj = json.loads(kubectl("get", "nodes", "-o", "json"))
    result = {}
    for n in obj["items"]:
        name = n["metadata"]["name"]
        ip = next((a["address"] for a in n["status"].get("addresses", [])
                   if a["type"] == "InternalIP"), "")
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in n["status"].get("conditions", []))
        result[name] = {"ip": ip, "ready": ready,
                        "runtime": n["status"].get("nodeInfo", {}).get("containerRuntimeVersion", "")}
    return result

def remote(worker, ip, command, args, timeout=120, check=True):
    target = args.ssh_targets.get(worker, ip)
    if "@" not in target:
        target = args.ssh_user + "@" + target
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
           "-o", "StrictHostKeyChecking=accept-new"]
    if args.ssh_key:
        cmd += ["-i", args.ssh_key]
    cmd += [target, "bash -lc " + shlex.quote(command)]
    p = call(cmd, timeout=timeout, check=False)
    if check and p.returncode:
        raise RuntimeError("SSH failed for " + worker + " (" + target + ")\n" +
                           p.stderr[-1800:] + "\nCommand: " + command)
    return p

def parse_digests(content_ls):
    return set(re.findall(r"(?m)^(sha256:[0-9a-f]{64})\s", content_ls))


def parse_image_rows(image_text):
    rows = []
    for line in image_text.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 3 and cols[0] not in ("REF",):
            rows.append({"ref": cols[0], "digest": cols[2]})
    return rows

def normalize_source_ref(source):
    raw = source.removeprefix("docker.io/").removeprefix("index.docker.io/")
    if "/" not in raw:
        raw = "library/" + raw
    return "docker.io/" + raw

def find_existing_ref(image, image_text):
    rows = parse_image_rows(image_text)
    source_ref = normalize_source_ref(image.get("source", ""))
    exact = [r["ref"] for r in rows if r["ref"] == source_ref]
    if exact:
        return exact[0]
    digests = {
        image.get("index_digest", ""),
        image.get("platform_manifest_digest", ""),
        image.get("image_ref", "").rsplit("@", 1)[-1],
    }
    digests.discard("")
    by_digest = [r["ref"] for r in rows if r["digest"] in digests]
    if by_digest:
        return by_digest[0]
    repo = source_ref.rsplit(":", 1)[0] if ":" in source_ref.rsplit("/", 1)[-1] else source_ref
    by_repo = [r["ref"] for r in rows if r["ref"].startswith(repo)]
    if by_repo:
        return by_repo[0]
    raise RuntimeError("No matching local image reference found for " + source_ref)

def refs_to_remove(image, image_text):
    rows = parse_image_rows(image_text)
    source_ref = normalize_source_ref(image.get("source", ""))
    repo = source_ref.rsplit(":", 1)[0] if ":" in source_ref.rsplit("/", 1)[-1] else source_ref
    digests = {
        image.get("index_digest", ""),
        image.get("platform_manifest_digest", ""),
        image.get("image_ref", "").rsplit("@", 1)[-1],
    }
    digests.discard("")
    refs = []
    for row in rows:
        ref = row["ref"]
        if ref == source_ref:
            refs.append(ref)
        elif ref.startswith(repo + "@") and row["digest"] in digests:
            refs.append(ref)
    return sorted(set(refs))

def blob_path(digest):
    return "/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256/" + digest.split(":", 1)[-1]

def blob_size(worker, ip, digest, args):
    out = remote(worker, ip, "sudo -n stat -c %s " + shlex.quote(blob_path(digest)),
                 args, timeout=20, check=False).stdout.strip()
    return int(out) if out.isdigit() else 0

def restore_image(worker, ip, image, args, proxy_port=None):
    source_ref = normalize_source_ref(image.get("source", image["image_ref"]))
    env = ""
    if proxy_port:
        env = (f"HTTP_PROXY=http://127.0.0.1:{proxy_port} "
               f"HTTPS_PROXY=http://127.0.0.1:{proxy_port} ")
    cmd = (f"sudo -n env {env} ctr -n k8s.io images pull "
           f"--platform linux/amd64 {shlex.quote(source_ref)}")
    return remote(worker, ip, cmd, args, timeout=900, check=False)

def node_inventory(worker, ip, args):
    env = remote(worker, ip,
        "sudo -n systemctl show containerd -p Environment --value", args).stdout.strip()
    ls = remote(worker, ip, "sudo -n ctr -n k8s.io content ls", args, timeout=120).stdout
    images = remote(worker, ip, "sudo -n ctr -n k8s.io images ls", args, timeout=120).stdout
    tcp = remote(worker, ip, "command -v tcpdump || true; sudo -n true && echo SUDO_OK; "
                 "command -v ctr || true", args).stdout
    m = re.search(r"(?:HTTP|HTTPS)_PROXY=(?:http://)?127\.0\.0\.1:(\d+)", env, re.I)
    return {"worker": worker, "ip": ip, "environment": env,
            "content_text": ls, "digests": parse_digests(ls),
            "images_text": images, "tcpdump": "/tcpdump" in tcp or "tcpdump" in tcp,
            "sudo_ok": "SUDO_OK" in tcp, "proxy_port": int(m.group(1)) if m else None,
            "runtime": get_nodes()[worker]["runtime"]}

def content_ref_count(content_text, digest):
    # Diagnostic heuristic only: a digest appearing on its own row plus a
    # manifest GC label is still referenced. Never delete a referenced blob.
    return sum(line.count(digest) for line in content_text.splitlines())

def choose_candidate(catalog, inventories):
    images = catalog.get("images", [])
    freq = Counter(l["digest"] for im in images for l in im.get("layers", []))
    candidates = []
    for im in images:
        ref = im.get("image_ref", "")
        if not any(s in (ref + " " + im.get("source", "")).lower() for s in SHELL_IMAGES):
            continue
        for layer in im.get("layers", []):
            d = layer["digest"]
            if freq[d] != 1:
                continue
            if not all(d in inv["digests"] for inv in inventories.values()):
                continue
            # Avoid deleting a blob referenced by any additional image manifest.
            if not all(content_ref_count(inv["content_text"], d) == 2
                       for inv in inventories.values()):
                continue
            f_hist = freq[d]
            f_slot = sum(1 for x in images if any(y["digest"] == d for y in x.get("layers", [])))
            centrality = freq[d]
            # Same PGDSF layer_priority structure with beta=2, rho=0.2;
            # bandwidth term uses the experiment's declared 10 MB/s. Historical
            # access is approximated by catalog occurrence, not claimed as live history.
            score = ((f_hist + 2.0 * f_slot) * (1.0 + 0.2 * centrality)) / 10.0
            candidates.append((score, int(layer["size_bytes"]), ref, d, im, layer))
    if not candidates:
        raise RuntimeError(
            "No safe common candidate found. Need a shell-capable catalog image "
            "with a layer present on all workers and referenced by exactly one image manifest. "
            "Run --mode inventory and inspect inventory.json; no cache was changed.")
    # Among equal low-priority candidates, prefer a larger blob so that the
    # transfer signal is measurable; the score remains the primary key.
    candidates.sort(key=lambda x: (x[0], -x[1], x[3]))
    score, size, ref, digest, image, layer = candidates[0]
    return {"image_ref": ref, "source": image.get("source", ref),
            "index_digest": image.get("index_digest", ""),
            "platform_manifest_digest": image.get("platform_manifest_digest", ""),
            "layer_digest": digest, "layer_size_bytes": size,
            "image_layers": [{"digest": x["digest"], "size_bytes": int(x["size_bytes"])}
                             for x in image.get("layers", [])],
            "layer_size_MB_decimal": size / 1_000_000,
            "pgdsf_score_proxy": score,
            "score_assumption": "f_hist/f_slot/centrality approximated from catalog occurrence; not a full online PGDSF state replay"}

def node_network(worker):
    try:
        obj = json.loads(kubectl("get", "--raw",
            f"/api/v1/nodes/{worker}/proxy/stats/summary", timeout=30))
        n = obj.get("node", {}).get("network", {})
        return {"rx": n.get("rxBytes"), "tx": n.get("txBytes")}
    except Exception:
        return {"rx": None, "tx": None}

def start_capture(worker, ip, inv, args, rid):
    port = inv["proxy_port"]
    if port:
        iface, direction, filt = "lo", "out", f"tcp src port {port}"
    else:
        iface, direction, filt = "any", "in", "tcp src port 443"
    base = f"/tmp/falcons-xfer-{rid}-{worker}"
    pcap, pidfile, logfile = base + ".pcap", base + ".pid", base + ".log"
    inner = (f"nohup tcpdump -Q {direction} -i {iface} -nn -s 0 -U "
             f"-w {pcap} {shlex.quote(filt)} >{logfile} 2>&1 </dev/null & "
             f"echo $! > {pidfile}")
    remote(worker, ip, "sudo -n sh -c " + shlex.quote(inner) +
           f"; sleep 1; sudo -n sh -c " + shlex.quote(f"kill -0 $(cat {pidfile})"),
           args, timeout=20)
    return {"pcap": pcap, "pidfile": pidfile, "logfile": logfile,
            "filter": filt, "interface": iface, "direction": direction}

def stop_capture(worker, ip, cap, args):
    stop = (f"kill -INT $(cat {cap['pidfile']}) 2>/dev/null || true; sleep 1; "
            f"tcpdump -nn -tt -r {cap['pcap']} {shlex.quote(cap['filter'])} 2>/dev/null || true")
    p = remote(worker, ip, "sudo -n sh -c " + shlex.quote(stop), args, timeout=45, check=False)
    payloads = [int(x) for x in re.findall(r"\blength\s+(\d+)", p.stdout)]
    return {"proxy_tcp_payload_bytes": sum(payloads),
            "proxy_tcp_payload_packets": len(payloads),
            "capture_error": p.stderr[-300:] if p.returncode else ""}

def delete_layer(worker, ip, digest, args):
    before = remote(worker, ip,
        f"sudo -n stat -c %s /var/lib/containerd/io.containerd.content.v1.content/blobs/sha256/{digest.split(':',1)[1]}",
        args, check=False).stdout.strip()
    if not before.isdigit() or int(before) <= 0:
        raise RuntimeError(worker + ": cannot verify blob file size before eviction for " + digest)
    remote(worker, ip, f"sudo -n ctr -n k8s.io content rm {shlex.quote(digest)}", args, timeout=60)
    ls = remote(worker, ip, "sudo -n ctr -n k8s.io content ls", args, timeout=120).stdout
    after = remote(worker, ip,
        f"sudo -n stat -c %s /var/lib/containerd/io.containerd.content.v1.content/blobs/sha256/{digest.split(':',1)[1]}",
        args, check=False).stdout.strip()
    if digest in parse_digests(ls) or after.isdigit():
        raise RuntimeError(worker + ": content rm did not physically remove " + digest +
                           "; aborting without further changes")
    return int(before)

def pull_pods(namespace, image_ref, workers, args, rid, timeout):
    ns_obj = {"apiVersion":"v1","kind":"Namespace","metadata":{"name":namespace,
        "labels":{"app.kubernetes.io/part-of":"falcons-transfer-metrics"}}}
    # Create the namespace through JSON stdin.
    p = subprocess.run(["kubectl", "create", "-f", "-"], input=json.dumps(ns_obj),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if p.returncode:
        raise RuntimeError("namespace create failed: " + p.stderr)
    manifest = []
    for i, worker in enumerate(workers, 1):
        manifest.append({
          "apiVersion":"v1","kind":"Pod",
          "metadata":{"name":f"pull-{rid}-w{i}","namespace":namespace,
                      "labels":{"app.kubernetes.io/part-of":"falcons-transfer-metrics"}},
          "spec":{"nodeName":worker,"restartPolicy":"Never","activeDeadlineSeconds":timeout,
                  "containers":[{"name":"pull-test","image":image_ref,"imagePullPolicy":"Always",
                    "command":["/bin/sh","-c"],"args":["sleep 2"],
                    "resources":{"requests":{"cpu":"20m","memory":"24Mi"},
                                 "limits":{"cpu":"100m","memory":"64Mi"}}}]}})
    for pod in manifest:
        p = subprocess.run(["kubectl","create","-f","-"],input=json.dumps(pod),
            text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        if p.returncode:
            raise RuntimeError("pod create failed: " + p.stderr)
    start = time.monotonic()
    last = {}
    while time.monotonic() - start < timeout:
        obj = json.loads(kubectl("-n", namespace, "get", "pods", "-o", "json"))
        done = True
        for p in obj.get("items", []):
            name = p["metadata"]["name"]
            phase = p.get("status", {}).get("phase", "Unknown")
            waiting = []
            for c in p.get("status", {}).get("containerStatuses", []):
                w = c.get("state", {}).get("waiting") or {}
                if w.get("reason") in ("ErrImagePull","ImagePullBackOff","InvalidImageName"):
                    waiting.append(w.get("reason"))
            state = (phase, ",".join(waiting))
            if last.get(name) != state:
                print("[POD]", name, state, flush=True); last[name] = state
            if phase not in ("Succeeded","Failed") and not waiting:
                done = False
        if done:
            break
        time.sleep(2)
    pods = json.loads(kubectl("-n", namespace, "get", "pods", "-o", "json"))
    events = json.loads(kubectl("-n", namespace, "get", "events", "-o", "json"))
    return pods, events

def parse_pull_seconds(message):
    m = re.search(r"Successfully pulled image .*? in (?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s", message)
    if not m:
        return None
    return int(m.group(1) or 0)*3600 + int(m.group(2) or 0)*60 + float(m.group(3))

def measure_unpack(worker, ip, image_ref, args, rid, out):
    ns = f"falcons-metrics-{rid[:6]}-{worker[-2:]}"
    remote(worker, ip, f"sudo -n ctr namespaces create {shlex.quote(ns)}", args, check=False)
    inv = remote(worker, ip, "sudo -n systemctl show containerd -p Environment --value", args).stdout
    m = re.search(r"(?:HTTP|HTTPS)_PROXY=(?:http://)?127\.0\.0\.1:(\d+)", inv, re.I)
    proxy = m.group(1) if m else ""
    env = (f"HTTP_PROXY=http://127.0.0.1:{proxy} HTTPS_PROXY=http://127.0.0.1:{proxy} "
           "NO_PROXY=localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16")
    def timed(label, command):
        logfile = f"/tmp/{label}-{rid}.log"
        shell = (f"t0=$(date +%s%N); {command} >{logfile} 2>&1; rc=$?; "
                 f"t1=$(date +%s%N); echo ELAPSED_MS=$(((t1-t0)/1000000)); "
                 f"echo RC=$rc; cat {logfile}; exit $rc")
        p = remote(worker, ip, "sudo -n sh -c " + shlex.quote(shell), args,
                    timeout=900, check=False)
        ms = re.search(r"ELAPSED_MS=(\d+)", p.stdout)
        rc = re.search(r"RC=(\d+)", p.stdout)
        return {"elapsed_ms": int(ms.group(1)) if ms else None,
                "return_code": int(rc.group(1)) if rc else p.returncode,
                "output_tail": p.stdout[-500:], "stderr": p.stderr[-300:]}
    pull = timed("falcons-skip-unpack", f"env {env} ctr -n {ns} images pull --skip-unpack --platform linux/amd64 {shlex.quote(image_ref)}")
    unpack = timed("falcons-unpack", f"ctr -n {ns} images unpack --snapshotter overlayfs --platform linux/amd64 {shlex.quote(image_ref)}") if pull["return_code"] == 0 else {"elapsed_ms":None,"return_code":-1,"output_tail":"pull failed","stderr":""}
    return {"timestamp_utc":stamp(),"worker":worker,"runtime":get_nodes()[worker]["runtime"],
            "image_ref":image_ref,"containerd_namespace":ns,
            "pull_skip_unpack_ms":pull["elapsed_ms"],"pull_return_code":pull["return_code"],
            "unpack_overlayfs_ms":unpack["elapsed_ms"],"unpack_return_code":unpack["return_code"],
            "pull_output_tail":pull["output_tail"],"unpack_output_tail":unpack["output_tail"],
            "interpretation":"unpack runs in a fresh containerd namespace with cached OCI blobs; this isolates unpack/snapshot preparation, not end-to-end cold pull"}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default="results/comnet_revision/k8s_real_testbed/oci_v2_12/catalog.json")
    ap.add_argument("--workers", nargs="+", default=DEFAULT_WORKERS)
    ap.add_argument("--ssh-user", default="ubuntu")
    ap.add_argument("--ssh-key", default="")
    ap.add_argument("--ssh-targets", nargs="*", default=[],
                    help="Optional worker=host or worker=user@host overrides")
    ap.add_argument("--mode", choices=["inventory","unpack","contention","all"], default="all")
    ap.add_argument("--apply-eviction", action="store_true",
                    help="Required for contention/all; physically deletes one verified layer blob then checks repull")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--out-root", default="results/comnet_revision/k8s_real_testbed/transfer_cache_metrics")
    a = ap.parse_args()
    if a.timeout < 60 or a.timeout > 1200:
        ap.error("--timeout must be 60..1200")
    if a.mode in ("contention","all") and not a.apply_eviction:
        ap.error("contention/all physically evict one test blob; pass --apply-eviction explicitly")
    raw_ssh_targets = a.ssh_targets
    a.ssh_targets = {}
    for entry in raw_ssh_targets:
        if "=" in entry:
            k,v = entry.split("=",1); a.ssh_targets[k] = v
    catalog = json.loads(pathlib.Path(a.catalog).read_text(encoding="utf-8"))
    if catalog.get("errors") or len(catalog.get("images", [])) < 1:
        ap.error("catalog must contain verified images and no errors")
    nodes = get_nodes()
    for w in a.workers:
        if w not in nodes or not nodes[w]["ready"]:
            ap.error("worker missing or not Ready: " + w)
    rid = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    out = pathlib.Path(a.out_root).resolve() / ("run-" + rid)
    out.mkdir(parents=True, exist_ok=False)
    inventories = {}
    for w in a.workers:
        inv = node_inventory(w, nodes[w]["ip"], a)
        inventories[w] = inv
        print("[NODE]", w, nodes[w]["ip"], nodes[w]["runtime"],
              "tcpdump", inv["tcpdump"], "proxy_port", inv["proxy_port"], flush=True)
        if not inv["sudo_ok"]:
            raise SystemExit("sudo -n failed on " + w + "; configure non-interactive sudo or run the commands manually")
        if a.mode in ("contention","all") and not inv["tcpdump"]:
            raise SystemExit("tcpdump missing on " + w + "; install tcpdump on all workers before contention measurement")
    jsonwrite(out/"inventory.json", {w:{"ip":v["ip"],"runtime":v["runtime"],
        "digests_present":len(v["digests"]),"tcpdump":v["tcpdump"],
        "proxy_port":v["proxy_port"],"image_inventory_tail":v["images_text"][-2000:]}
        for w,v in inventories.items()})
    invrows = []
    for w,v in inventories.items():
        for im in catalog["images"]:
            for layer in im.get("layers",[]):
                invrows.append({"worker":w,"image_ref":im.get("image_ref",""),
                    "layer_digest":layer["digest"],"compressed_bytes":layer["size_bytes"],
                    "present":layer["digest"] in v["digests"],
                    "content_ref_line_count":content_ref_count(v["content_text"],layer["digest"])})
    csvwrite(out/"layer_inventory.csv",invrows)
    print("[SAVED]",out,flush=True)
    if a.mode == "inventory":
        return 0
    target = choose_candidate(catalog, inventories)
    jsonwrite(out/"selected_pgdsf_layer.json", target)
    print("[TARGET]",target["image_ref"],target["layer_digest"],
          "bytes",target["layer_size_bytes"],"PGDSF-score-proxy",target["pgdsf_score_proxy"],flush=True)
    if a.mode in ("unpack","all"):
        unpackrows = []
        for w in a.workers:
            print("[UNPACK]",w,flush=True)
            row = measure_unpack(w,nodes[w]["ip"],target["image_ref"],a,rid,out)
            unpackrows.append(row)
            print("  pull_skip_unpack_ms",row["pull_skip_unpack_ms"],
                  "unpack_overlayfs_ms",row["unpack_overlayfs_ms"],
                  "rc",row["pull_return_code"],row["unpack_return_code"],flush=True)
        csvwrite(out/"decompression_metrics.csv",unpackrows)
        jsonwrite(out/"decompression_metrics.json",unpackrows)
    if a.mode in ("contention","all"):
        active = json.loads(kubectl("get","pods","-A","-o","json"))
        target_manifest_digest = target["image_ref"].rsplit("@",1)[-1]
        for p in active.get("items",[]):
            phase = p.get("status",{}).get("phase","")
            if phase not in ("Pending","Running"):
                continue
            refs = [c.get("image","") for c in p.get("spec",{}).get("containers",[])]
            ids = [c.get("imageID","") for c in p.get("status",{}).get("containerStatuses",[])]
            if (target["image_ref"] in refs or
                any(target_manifest_digest in x for x in ids)):
                raise SystemExit("Active Pod may use target image/layer; refusing physical eviction: " +
                                 p["metadata"]["namespace"] + "/" + p["metadata"]["name"])
        rows = []
        for concurrency in (1,2,4):
            workers = a.workers[:concurrency]
            print(f"[CONTENTION] concurrency={concurrency} workers={workers}",flush=True)
            before_net = {w:node_network(w) for w in workers}
            caps = {}
            for w in workers:
                caps[w] = start_capture(w,nodes[w]["ip"],inventories[w],a,rid+f"-c{concurrency}")
            evicted = {}
            try:
                try:
                    for w in workers:
                        evicted[w] = delete_layer(w,nodes[w]["ip"],target["layer_digest"],a)
                        print("[EVICTED]",w,target["layer_digest"],"physical_blob_bytes",evicted[w],flush=True)
                    ns = "falcons-xfer-" + uuid.uuid4().hex[:8]
                    pods,events = pull_pods(ns,target["image_ref"],workers,a,rid+f"c{concurrency}",a.timeout)
                    jsonwrite(out/f"pods_c{concurrency}.json",pods)
                    jsonwrite(out/f"events_c{concurrency}.json",events)
                    kubectl("delete","namespace",ns,"--wait=false",timeout=30)
                except Exception:
                    # Never leave a deliberately evicted test blob missing if a
                    # later worker eviction or the Kubernetes pull fails.
                    for rw in workers:
                        current = remote(rw,nodes[rw]["ip"],
                            "sudo -n ctr -n k8s.io content ls",a,timeout=120,check=False).stdout
                        if target["layer_digest"] not in parse_digests(current):
                            port = inventories[rw]["proxy_port"]
                            env = (f"HTTP_PROXY=http://127.0.0.1:{port} HTTPS_PROXY=http://127.0.0.1:{port} " if port else "")
                            restored = remote(rw,nodes[rw]["ip"],
                                f"sudo -n env {env} ctr -n k8s.io images pull --skip-unpack --platform linux/amd64 {shlex.quote(target['image_ref'])}",
                                a,timeout=900,check=False)
                            print("[ROLLBACK RESTORE]",rw,"rc",restored.returncode,restored.stderr[-300:],flush=True)
                    raise
            finally:
                capture = {w:stop_capture(w,nodes[w]["ip"],caps[w],a) for w in workers}
            after_net = {w:node_network(w) for w in workers}
            pod_by_node = {}
            for p in pods.get("items",[]):
                w = p.get("spec",{}).get("nodeName","")
                pod_by_node[w] = p
            event_items = events.get("items",[])
            for w in workers:
                phase = pod_by_node.get(w,{}).get("status",{}).get("phase","Missing")
                pod_name = pod_by_node.get(w,{}).get("metadata",{}).get("name","")
                durations = [parse_pull_seconds(e.get("message","")) for e in event_items
                    if e.get("reason") == "Pulled" and
                    e.get("involvedObject",{}).get("name") == pod_name]
                durations = [x for x in durations if x is not None]
                present_after = target["layer_digest"] in parse_digests(remote(w,nodes[w]["ip"],
                    "sudo -n ctr -n k8s.io content ls",a,timeout=120).stdout)
                rx0,rx1 = before_net[w]["rx"],after_net[w]["rx"]
                tx0,tx1 = before_net[w]["tx"],after_net[w]["tx"]
                rows.append({"timestamp_utc":stamp(),"concurrency":concurrency,"worker":w,
                    "runtime":nodes[w]["runtime"],"image_ref":target["image_ref"],
                    "evicted_layer_digest":target["layer_digest"],
                    "evicted_blob_bytes_observed":evicted.get(w),
                    "catalog_compressed_layer_bytes":target["layer_size_bytes"],
                    "blob_restored_after_pull":present_after,"pod_phase":phase,
                    "host_rx_delta_bytes_NOT_OCI":(rx1-rx0) if isinstance(rx0,int) and isinstance(rx1,int) else None,
                    "host_tx_delta_bytes_NOT_OCI":(tx1-tx0) if isinstance(tx0,int) and isinstance(tx1,int) else None,
                    **capture[w],
                    "pull_duration_event_s":durations[0] if durations else None,
                    "interpretation":"proxy TCP payload is measured on local proxy response path and includes protocol overhead; host RX/TX is unattributed background-inclusive traffic"})
                if not present_after:
                    print("[WARN] layer not restored after pull on",w,"attempting explicit restore",flush=True)
                    port = inventories[w]["proxy_port"]
                    env = (f"HTTP_PROXY=http://127.0.0.1:{port} HTTPS_PROXY=http://127.0.0.1:{port} " if port else "")
                    restore = remote(w,nodes[w]["ip"],
                        f"sudo -n env {env} ctr -n k8s.io images pull --skip-unpack --platform linux/amd64 {shlex.quote(target['image_ref'])}",
                        a,timeout=900,check=False)
                    print("[RESTORE]",w,"rc",restore.returncode,restore.stderr[-200:],flush=True)
        csvwrite(out/"transfer_contention_metrics.csv",rows)
        jsonwrite(out/"transfer_contention_metrics.json",rows)
        print("[CONTENTION SAVED]",out/"transfer_contention_metrics.csv",flush=True)
    jsonwrite(out/"run_metadata.json",{"created_utc":stamp(),"mode":a.mode,
        "workers":a.workers,"catalog":a.catalog,"target":target if a.mode!="inventory" else None,
        "limitations":["Node RX/TX deltas are not registry-only bytes.",
        "Proxy TCP payload counts response payload on the configured proxy loopback path and includes TLS/HTTP protocol overhead.",
        "Physical eviction is a one-layer mechanism validation, not evidence that containerd natively implements PGDSF.",
        "PGDSF score is a catalog-derived proxy because live scheduler history/future-slot state is not observable here."]})
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("[FATAL]",type(exc).__name__,str(exc),file=sys.stderr,flush=True)
        sys.exit(2)
