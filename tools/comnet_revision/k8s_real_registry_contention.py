#!/usr/bin/env python3
"""Measure concurrent real OCI image pulls without deleting containerd content.

One run uses distinct, immutable image digests for the 1/2/4 concurrency
levels. Before any Pods are created, the script inventories each worker's containerd
content records and physical blobs. It chooses distinct immutable images with
at least one missing layer, records each image's exact missing-layer bytes,
and avoids reusing a missing layer on the same worker across levels.

This is a first-pass, one-trial-per-level experiment. It deliberately does not
evict content or claim native PGDSF behavior. Registry TCP payload is measured
on each worker's local proxy loopback path (tcpdump response direction).
"""
import argparse
import csv
import datetime as dt
import json
import pathlib
import re
import shlex
import subprocess
import sys
import time
import uuid
from collections import Counter

DEFAULT_WORKERS = [
    "falcons-worker1", "falcons-worker2",
    "falcons-worker3", "falcons-worker4",
]
SHELL_FAMILIES = ("alpine", "busybox", "debian", "ubuntu",
                  "python", "node", "golang", "ruby", "php", "haproxy",
                  "caddy", "memcached")


def run(cmd, *, input_text=None, timeout=60, check=True):
    p = subprocess.run(
        cmd, input=input_text, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout
    )
    if check and p.returncode:
        raise RuntimeError(
            "$ " + " ".join(cmd) + "\n" + p.stderr[-1600:]
        )
    return p


def kubectl(*args, input_text=None, timeout=60, check=True):
    return run(
        ["kubectl", "--request-timeout=30s", *args],
        input_text=input_text, timeout=timeout, check=check
    )


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def ssh(worker, ip, command, args, timeout=120, check=True):
    target = args.ssh_targets.get(worker, ip)
    if "@" not in target:
        target = args.ssh_user + "@" + target
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if args.ssh_key:
        cmd += ["-i", args.ssh_key]
    cmd += [target, "bash -lc " + shlex.quote(command)]
    p = run(cmd, timeout=timeout, check=False)
    if check and p.returncode:
        raise RuntimeError(
            "SSH failed for " + worker + " (" + target + ")\n" +
            p.stderr[-1600:] + "\nCommand: " + command
        )
    return p


def get_nodes(workers):
    obj = json.loads(kubectl("get", "nodes", "-o", "json").stdout)
    found = {}
    for n in obj.get("items", []):
        name = n["metadata"]["name"]
        ip = next(
            (x["address"] for x in n["status"].get("addresses", [])
             if x.get("type") == "InternalIP"), ""
        )
        ready = any(
            x.get("type") == "Ready" and x.get("status") == "True"
            for x in n["status"].get("conditions", [])
        )
        found[name] = {
            "ip": ip, "ready": ready,
            "runtime": n["status"].get("nodeInfo", {}).get(
                "containerRuntimeVersion", ""
            )
        }
    for w in workers:
        if w not in found or not found[w]["ready"] or not found[w]["ip"]:
            raise RuntimeError("Worker missing/not Ready/no InternalIP: " + w)
    return found


def content_record_digests(text):
    out = set()
    for line in text.splitlines()[1:]:
        cols = line.split()
        if cols and re.fullmatch(r"sha256:[0-9a-f]{64}", cols[0]):
            out.add(cols[0])
    return out


def image_rows(text):
    rows = []
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 3:
            rows.append({"ref": cols[0], "digest": cols[2]})
    return rows


def get_inventory(worker, ip, catalog, args):
    all_digests = sorted({
        layer["digest"].split(":", 1)[1]
        for image in catalog.get("images", [])
        for layer in image.get("layers", [])
        if re.fullmatch(r"sha256:[0-9a-f]{64}", layer.get("digest", ""))
    })
    blob_checks = []
    for h in all_digests:
        path = "/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256/" + h
        blob_checks.append(
            "if sudo -n test -s " + shlex.quote(path) +
            "; then printf 'BLOB_PRESENT sha256:" + h + " '; " +
            "sudo -n stat -c %s " + shlex.quote(path) + "; fi"
        )
    command = (
        "set -eu; "
        "echo __ENV__; sudo -n systemctl show containerd -p Environment --value; "
        "echo __IMAGES__; sudo -n ctr -n k8s.io images ls; "
        "echo __CONTENT__; sudo -n ctr -n k8s.io content ls; "
        "echo __BLOBS__; " + "; ".join(blob_checks) + "; "
        "echo __TOOLS__; command -v tcpdump || true; sudo -n true && echo SUDO_OK"
    )
    p = ssh(worker, ip, command, args, timeout=180)
    sections = {}
    current = None
    for line in p.stdout.splitlines():
        if line in ("__ENV__", "__IMAGES__", "__CONTENT__", "__BLOBS__", "__TOOLS__"):
            current = line.strip("_")
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    env_text = "\n".join(sections.get("ENV", []))
    image_text = "\n".join(sections.get("IMAGES", []))
    content_text = "\n".join(sections.get("CONTENT", []))
    blobs_text = "\n".join(sections.get("BLOBS", []))
    tools_text = "\n".join(sections.get("TOOLS", []))
    proxy_match = re.search(
        r"(?:HTTP|HTTPS)_PROXY=(?:http://)?127\.0\.0\.1:(\d+)",
        env_text, re.I
    )
    proxy_port = int(proxy_match.group(1)) if proxy_match else None
    blob_sizes = {}
    for line in blobs_text.splitlines():
        m = re.match(r"BLOB_PRESENT (sha256:[0-9a-f]{64}) (\d+)", line.strip())
        if m:
            blob_sizes[m.group(1)] = int(m.group(2))
    return {
        "worker": worker, "ip": ip, "runtime": get_nodes([worker])[worker]["runtime"],
        "proxy_port": proxy_port, "tcpdump": "tcpdump" in tools_text,
        "sudo_ok": "SUDO_OK" in tools_text,
        "image_text": image_text, "image_rows": image_rows(image_text),
        "content_text": content_text, "content_digests": content_record_digests(content_text),
        "blob_sizes": blob_sizes,
    }


def is_shell_image(image):
    text = (image.get("source", "") + " " + image.get("image_ref", "")).lower()
    return any(name in text for name in SHELL_FAMILIES) and bool(image.get("layers"))


def make_plan(catalog, inventories, workers):
    """Plan 1 + 2 + 4 unique image pulls with verified missing-layer sets."""
    used_refs = set()
    used_layers_by_worker = {w: set() for w in workers}
    plan = {}
    for concurrency in (1, 2, 4):
        slots = []
        for worker in workers[:concurrency]:
            inv = inventories[worker]
            chosen = None
            chosen_missing = []
            for image in catalog.get("images", []):
                ref = image.get("image_ref", "")
                manifest_digests = {
                    image.get("platform_manifest_digest", ""),
                    image.get("index_digest", ""),
                }
                manifest_digests.discard("")
                if not ref or "@sha256:" not in ref or ref in used_refs:
                    continue
                if not is_shell_image(image):
                    continue
                layers = [x.get("digest", "") for x in image.get("layers", [])]
                if not layers or any(not d.startswith("sha256:") for d in layers):
                    continue
                if any(d in inv["content_digests"] for d in manifest_digests):
                    continue
                if any(row["ref"] == ref or row["digest"] in manifest_digests
                       for row in inv["image_rows"]):
                    continue
                missing = [
                    d for d in layers
                    if d not in inv["content_digests"] and d not in inv["blob_sizes"]
                ]
                if not missing:
                    continue
                if set(missing) & used_layers_by_worker[worker]:
                    continue
                chosen = image
                chosen_missing = missing
                break
            if chosen is None:
                raise RuntimeError(
                    "Preflight found too few image references with missing compressed "
                    "layers for concurrency 1/2/4 without reusing a missing layer on "
                    "the same worker. No Pod has been created and no cache was changed. "
                    "Audit a larger OCI catalog, then rerun. Worker needing a candidate: "
                    + worker
                )
            used_refs.add(chosen["image_ref"])
            used_layers_by_worker[worker].update(chosen_missing)
            layer_sizes = {x["digest"]: int(x["size_bytes"]) for x in chosen["layers"]}
            slots.append({
                "worker": worker, "image_ref": chosen["image_ref"],
                "source": chosen.get("source", ""),
                "manifest_digest": chosen.get("platform_manifest_digest", ""),
                "layer_count": len(chosen["layers"]),
                "preexisting_layer_count": len(chosen["layers"]) - len(chosen_missing),
                "missing_layer_count": len(chosen_missing),
                "compressed_layer_bytes": sum(layer_sizes.values()),
                "missing_layer_bytes": sum(layer_sizes[d] for d in chosen_missing),
                "layer_digests": [x["digest"] for x in chosen["layers"]],
                "missing_layer_digests": chosen_missing,
            })
        plan[str(concurrency)] = slots
    return plan


def start_capture(worker, ip, port, rid, args):
    base = "/tmp/falcons-regcomp-" + rid + "-" + worker
    pcap, pidfile, logfile = base + ".pcap", base + ".pid", base + ".log"
    inner = (
        "nohup tcpdump -Q out -i lo -nn -s 0 -U -w " + shlex.quote(pcap) +
        " 'tcp src port " + str(port) + "' >" + shlex.quote(logfile) +
        " 2>&1 </dev/null & echo $! > " + shlex.quote(pidfile)
    )
    ssh(
        worker, ip,
        "sudo -n sh -c " + shlex.quote(inner) + "; sleep 1; " +
        "sudo -n sh -c " + shlex.quote("kill -0 $(cat " + pidfile + ")"),
        args, timeout=25
    )
    return {"pcap": pcap, "pidfile": pidfile, "logfile": logfile, "port": port}


def stop_capture(worker, ip, cap, local_pcap, args):
    stop = (
        "sudo -n sh -c " + shlex.quote(
            "kill -INT $(cat " + cap["pidfile"] + ") 2>/dev/null || true"
        ) + "; sleep 1; sudo -n tcpdump -nn -tt -r " +
        shlex.quote(cap["pcap"]) + " 'tcp src port " + str(cap["port"]) + "' 2>/dev/null"
    )
    p = ssh(worker, ip, stop, args, timeout=120, check=False)
    payloads = [int(x) for x in re.findall(r"\blength\s+(\d+)", p.stdout)]
    packets = len(payloads)
    bytes_total = sum(payloads)

    # Preserve raw PCAP locally when SSH/SCP access permits it.
    copied = False
    chmod = ssh(worker, ip, "sudo -n chmod 644 " + shlex.quote(cap["pcap"]), args,
                timeout=20, check=False)
    target = args.ssh_targets.get(worker, ip)
    if "@" not in target:
        target = args.ssh_user + "@" + target
    scp_cmd = [
        "scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if args.ssh_key:
        scp_cmd += ["-i", args.ssh_key]
    scp_cmd += [target + ":" + cap["pcap"], str(local_pcap)]
    cp = run(scp_cmd, timeout=180, check=False)
    copied = cp.returncode == 0
    if copied:
        ssh(worker, ip, "sudo -n rm -f " + " ".join(
            shlex.quote(x) for x in (cap["pcap"], cap["pidfile"], cap["logfile"])
        ), args, timeout=20, check=False)
    return {
        "proxy_tcp_payload_bytes": bytes_total,
        "proxy_tcp_payload_packets": packets,
        "pcap_local": str(local_pcap) if copied else "",
        "pcap_copy_error": "" if copied else cp.stderr[-400:],
        "capture_stderr": p.stderr[-400:] if p.returncode else "",
    }


def parse_pull_seconds(message):
    m = re.search(
        r"Successfully pulled image .*? in (?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s",
        message
    )
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3))


def run_level(concurrency, slots, nodes, args, root, run_id):
    namespace = "falcons-regcomp-" + run_id[:6] + "-c" + str(concurrency)
    namespace_obj = {
        "apiVersion": "v1", "kind": "Namespace",
        "metadata": {"name": namespace,
                     "labels": {"app.kubernetes.io/part-of": "falcons-registry-contention"}}
    }
    kubectl("create", "-f", "-", input_text=json.dumps(namespace_obj))
    captures = {}
    pods_obj = {"apiVersion": "v1", "kind": "List", "items": []}
    for i, slot in enumerate(slots):
        pod_name = "pull-" + str(i + 1)
        pods_obj["items"].append({
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {
                "name": pod_name, "namespace": namespace,
                "labels": {"app.kubernetes.io/part-of": "falcons-registry-contention"}
            },
            "spec": {
                "nodeName": slot["worker"], "restartPolicy": "Never",
                "activeDeadlineSeconds": args.timeout,
                "containers": [{
                    "name": "pull-test", "image": slot["image_ref"],
                    "imagePullPolicy": "Always",
                    "command": ["/bin/sh", "-c"], "args": ["sleep 1"],
                    "resources": {
                        "requests": {"cpu": "20m", "memory": "24Mi"},
                        "limits": {"cpu": "100m", "memory": "64Mi"}
                    }
                }]
            }
        })
    for slot in slots:
        w = slot["worker"]
        captures[w] = start_capture(
            w, nodes[w]["ip"], args_inventory_proxy[ w ], run_id + "-c" + str(concurrency), args
        )
    start = time.monotonic()
    error = ""
    pod_obj = {"items": []}
    event_obj = {"items": []}
    try:
        kubectl("create", "-f", "-", input_text=json.dumps(pods_obj), timeout=60)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            pod_obj = json.loads(kubectl("-n", namespace, "get", "pods", "-o", "json").stdout)
            states = {}
            for pod in pod_obj.get("items", []):
                name = pod["metadata"]["name"]
                phase = pod.get("status", {}).get("phase", "Unknown")
                waiting = []
                for c in pod.get("status", {}).get("containerStatuses", []):
                    w = c.get("state", {}).get("waiting") or {}
                    if w.get("reason") in ("ErrImagePull", "ImagePullBackOff",
                                            "InvalidImageName", "ImageInspectError"):
                        waiting.append(w.get("reason"))
                states[name] = phase
                if waiting or phase == "Failed":
                    raise RuntimeError(
                        name + " phase=" + phase + " waiting=" + ",".join(waiting)
                    )
            if len(states) == concurrency and all(x == "Succeeded" for x in states.values()):
                break
            time.sleep(1)
        else:
            raise RuntimeError("timeout waiting for all Pods to succeed")
    except Exception as exc:
        error = str(exc)
    finally:
        wall_s = round(time.monotonic() - start, 4)
        pod_obj = json.loads(kubectl(
            "-n", namespace, "get", "pods", "-o", "json", check=False
        ).stdout or '{"items":[]}')
        event_obj = json.loads(kubectl(
            "-n", namespace, "get", "events", "-o", "json", check=False
        ).stdout or '{"items":[]}')
        write_json(root / ("pods_c" + str(concurrency) + ".json"), pod_obj)
        write_json(root / ("events_c" + str(concurrency) + ".json"), event_obj)
        capture_rows = {}
        for slot in slots:
            w = slot["worker"]
            local_pcap = root / ("capture_c" + str(concurrency) + "_" + w + ".pcap")
            capture_rows[w] = stop_capture(w, nodes[w]["ip"], captures[w], local_pcap, args)
        kubectl("delete", "namespace", namespace, "--wait=false", timeout=30, check=False)

    rows = []
    for slot in slots:
        w = slot["worker"]
        pod_name = "pull-" + str(slots.index(slot) + 1)
        pod = next((p for p in pod_obj.get("items", [])
                    if p.get("metadata", {}).get("name") == pod_name), {})
        phase = pod.get("status", {}).get("phase", "Missing")
        durations = []
        for event in event_obj.get("items", []):
            if (event.get("reason") == "Pulled" and
                event.get("involvedObject", {}).get("name") == pod_name):
                d = parse_pull_seconds(event.get("message", ""))
                if d is not None:
                    durations.append(d)
        rows.append({
            "timestamp_utc": stamp(), "concurrency": concurrency, "worker": w,
            "source_image": slot["source"], "image_ref": slot["image_ref"],
            "compressed_layer_bytes": slot["compressed_layer_bytes"],
            "layer_count": slot["layer_count"], "pod_name": pod_name,
            "pod_phase": phase, "pull_duration_event_s": durations[-1] if durations else None,
            "batch_wall_s": wall_s, "trial_error": error,
            **capture_rows[w],
        })
    return rows, error


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--workers", nargs="+", default=DEFAULT_WORKERS)
    ap.add_argument("--ssh-user", default="ubuntu")
    ap.add_argument("--ssh-key", default="")
    ap.add_argument("--ssh-targets", nargs="*", default=[],
                    help="Optional worker=host or worker=user@host overrides")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out-root",
                    default="results/comnet_revision/k8s_real_testbed/registry_contention")
    a = ap.parse_args()
    if a.timeout < 60 or a.timeout > 1200:
        ap.error("--timeout must be 60..1200")
    if len(a.workers) < 4:
        ap.error("At least four workers are required for the 1/2/4 concurrency plan")
    a.ssh_targets = {
        x.split("=", 1)[0]: x.split("=", 1)[1]
        for x in a.ssh_targets if "=" in x
    }
    catalog_path = pathlib.Path(a.catalog)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if catalog.get("errors") or len(catalog.get("images", [])) < 7:
        ap.error("Catalog must have at least seven verified images and no errors")
    nodes = get_nodes(a.workers)
    rid = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    root = pathlib.Path(a.out_root).resolve() / ("run-" + rid)
    root.mkdir(parents=True, exist_ok=False)

    global args_inventory_proxy
    inventories = {}
    for w in a.workers:
        inv = get_inventory(w, nodes[w]["ip"], catalog, a)
        if not inv["sudo_ok"] or not inv["tcpdump"] or not inv["proxy_port"]:
            raise RuntimeError(
                "Preflight failed on " + w +
                ": requires non-interactive sudo, tcpdump, and containerd localhost proxy"
            )
        inventories[w] = inv
        print("[NODE]", w, inv["ip"], inv["runtime"],
              "proxy_port", inv["proxy_port"], "content_digests",
              len(inv["content_digests"]), "physical_blobs",
              len(inv["blob_sizes"]), flush=True)
    args_inventory_proxy = {w: inventories[w]["proxy_port"] for w in a.workers}
    write_json(root / "inventory_before.json", {
        w: {
            "ip": inv["ip"], "runtime": inv["runtime"],
            "proxy_port": inv["proxy_port"],
            "content_digest_count": len(inv["content_digests"]),
            "physical_blob_count_for_catalog": len(inv["blob_sizes"]),
            "catalog_layer_digests_present": sorted(
                set(inv["content_digests"]) | set(inv["blob_sizes"])
            ),
        } for w, inv in inventories.items()
    })

    plan = make_plan(catalog, inventories, a.workers)
    write_json(root / "plan.json", plan)
    print("[PREFLIGHT PASS] planned unique, uncached image pulls for concurrency 1/2/4",
          flush=True)
    for level, slots in plan.items():
        print("[PLAN]", level, [(x["worker"], x["source"], x["missing_layer_bytes"],
                                x["missing_layer_count"], "/", x["layer_count"])
                                for x in slots], flush=True)

    all_rows = []
    for level in (1, 2, 4):
        print("[RUN]", "concurrency", level, flush=True)
        rows, error = run_level(level, plan[str(level)], nodes, a, root, rid)
        all_rows.extend(rows)
        write_csv(root / "registry_contention_metrics.csv", all_rows)
        write_json(root / "registry_contention_metrics.json", all_rows)
        if error:
            print("[STOP] trial failed; evidence saved; not continuing with later levels:", error,
                  flush=True)
            break

    level_summary = []
    for level in (1, 2, 4):
        rows = [r for r in all_rows if r["concurrency"] == level]
        if not rows:
            continue
        level_summary.append({
            "concurrency": level,
            "pods": len(rows),
            "succeeded": sum(r["pod_phase"] == "Succeeded" for r in rows),
            "expected_compressed_layer_bytes": sum(r["compressed_layer_bytes"] for r in rows),
            "expected_missing_layer_bytes": sum(r["missing_layer_bytes"] for r in rows),
            "proxy_tcp_payload_bytes": sum(r["proxy_tcp_payload_bytes"] for r in rows),
            "batch_wall_s": max((r["batch_wall_s"] for r in rows), default=None),
            "mean_pull_duration_event_s": (
                sum(r["pull_duration_event_s"] for r in rows if r["pull_duration_event_s"] is not None) /
                sum(r["pull_duration_event_s"] is not None for r in rows)
                if any(r["pull_duration_event_s"] is not None for r in rows) else None
            ),
            "trial_error": next((r["trial_error"] for r in rows if r["trial_error"]), ""),
        })
    write_json(root / "registry_contention_level_summary.json", level_summary)
    write_csv(root / "registry_contention_level_summary.csv", level_summary)
    write_json(root / "run_metadata.json", {
        "timestamp_utc": stamp(), "catalog": str(catalog_path),
        "code_script": "k8s_real_registry_contention.py",
        "levels": [1, 2, 4], "repetitions_per_level": 1,
        "cache_precondition": "each selected image is checked before any Pods are created; missing layer digests are absent from containerd content records and physical blob paths; only those missing layers are counted as expected transfer bytes; missing layer digests are not reused on the same worker across levels",
        "capture": "tcpdump on worker loopback proxy response direction; TCP payload includes registry protocol/TLS overhead and is not identical to compressed layer bytes",
        "limitation": "different immutable image references are used at each slot to avoid destructive eviction; some layers may already be cached, so compare observed proxy payload against expected missing-layer bytes and repeat with a fresh catalog for statistical confidence",
        "output_dir": str(root),
    })
    print("[SAVED]", root, flush=True)
    print("[SUMMARY]", json.dumps(level_summary, ensure_ascii=False), flush=True)
    return 0 if level_summary and all(x["succeeded"] == x["pods"] for x in level_summary) else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("[FATAL]", type(exc).__name__, str(exc), file=sys.stderr, flush=True)
        sys.exit(2)
