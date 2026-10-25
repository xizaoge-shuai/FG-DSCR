#!/usr/bin/env python3
"""Sample actual Kubernetes node resources and compute a transparent imbalance proxy.

The script records scheduler-visible Pod requests and kubelet-observed CPU,
memory and node-filesystem measurements. Its residual-resource imbalance
statistic is a diagnostic proxy; it must not be substituted for the paper's
fragmentation definition unless the formulas are explicitly aligned.
"""
import argparse
import csv
import datetime as dt
import json
import math
import pathlib
import re
import subprocess
import sys
import time
from decimal import Decimal

DEFAULT_WORKERS = [
    "falcons-worker1", "falcons-worker2",
    "falcons-worker3", "falcons-worker4",
]
RESOURCE_KEYS = ("cpu", "memory", "ephemeral-storage")


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def call(cmd, timeout=30, check=True):
    p = subprocess.run(
        cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout
    )
    if check and p.returncode:
        raise RuntimeError("$ " + " ".join(cmd) + "\n" + p.stderr[-1200:])
    return p


def kubectl(*args, timeout=30, check=True):
    return call(["kubectl", "--request-timeout=20s", *args], timeout, check)


def parse_cpu(value):
    """Return CPU quantity in cores."""
    s = str(value or "0").strip()
    for suffix, factor in (("n", Decimal("1e-9")),
                           ("u", Decimal("1e-6")),
                           ("m", Decimal("1e-3"))):
        if s.endswith(suffix):
            return float(Decimal(s[:-len(suffix)]) * factor)
    return float(Decimal(s))


def parse_bytes(value):
    """Return a Kubernetes memory/storage quantity in bytes."""
    s = str(value or "0").strip()
    factors = {
        "Ki": Decimal(1024), "Mi": Decimal(1024) ** 2,
        "Gi": Decimal(1024) ** 3, "Ti": Decimal(1024) ** 4,
        "Pi": Decimal(1024) ** 5, "Ei": Decimal(1024) ** 6,
        "k": Decimal(1000), "K": Decimal(1000),
        "M": Decimal(1000) ** 2, "G": Decimal(1000) ** 3,
        "T": Decimal(1000) ** 4, "P": Decimal(1000) ** 5,
        "E": Decimal(1000) ** 6, "m": Decimal("1e-3"),
    }
    for suffix in sorted(factors, key=len, reverse=True):
        if s.endswith(suffix):
            return float(Decimal(s[:-len(suffix)]) * factors[suffix])
    return float(Decimal(s))


def quantity(value, resource):
    return parse_cpu(value) if resource == "cpu" else parse_bytes(value)


def container_requests(container):
    req = container.get("resources", {}).get("requests", {}) or {}
    return {r: quantity(req.get(r, "0"), r) for r in RESOURCE_KEYS}


def pod_effective_requests(pod):
    """Basic Kubernetes request accounting: max(sum regular, max init) + overhead."""
    spec = pod.get("spec", {})
    regular = {r: 0.0 for r in RESOURCE_KEYS}
    for c in spec.get("containers", []):
        cr = container_requests(c)
        for r in RESOURCE_KEYS:
            regular[r] += cr[r]
    init_max = {r: 0.0 for r in RESOURCE_KEYS}
    for c in spec.get("initContainers", []):
        cr = container_requests(c)
        for r in RESOURCE_KEYS:
            init_max[r] = max(init_max[r], cr[r])
    overhead = {
        r: quantity((spec.get("overhead", {}) or {}).get(r, "0"), r)
        for r in RESOURCE_KEYS
    }
    return {
        r: max(regular[r], init_max[r]) + overhead[r]
        for r in RESOURCE_KEYS
    }


def stddev(values):
    if not values:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((x - mean) ** 2 for x in values) / len(values))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", nargs="+", default=DEFAULT_WORKERS)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--duration", type=float, default=300.0)
    ap.add_argument("--timeout", type=int, default=12)
    ap.add_argument("--out-dir",
                    default="results/comnet_revision/k8s_real_testbed/resource_fragmentation")
    a = ap.parse_args()
    if len(a.workers) < 2:
        ap.error("At least two workers are required")
    if a.interval < 1 or a.duration < 2 or a.timeout < 3:
        ap.error("interval >= 1, duration >= 2, and timeout >= 3 are required")

    root = pathlib.Path(a.out_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    node_csv_path = root / "node_resource_snapshots.csv"
    metric_csv_path = root / "resource_imbalance_timeseries.csv"
    raw_path = root / "kubelet_summary_raw.jsonl"
    errors_path = root / "sampling_errors.jsonl"

    node_fields = [
        "timestamp_utc", "sample_index", "node", "runtime", "active_pod_count",
        "alloc_cpu_cores", "requested_cpu_cores", "requested_cpu_fraction",
        "actual_cpu_cores", "actual_cpu_fraction",
        "alloc_memory_bytes", "requested_memory_bytes", "requested_memory_fraction",
        "actual_memory_working_set_bytes", "actual_memory_fraction",
        "alloc_ephemeral_bytes", "requested_ephemeral_bytes", "requested_ephemeral_fraction",
        "node_fs_capacity_bytes", "node_fs_available_bytes", "node_fs_used_fraction",
    ]
    metric_fields = [
        "timestamp_utc", "sample_index", "worker_count",
        "stddev_free_cpu_fraction", "stddev_free_memory_fraction",
        "stddev_free_ephemeral_fraction", "mean_residual_imbalance_proxy",
        "min_free_cpu_fraction", "min_free_memory_fraction",
        "min_free_ephemeral_fraction",
    ]

    node_file = node_csv_path.open("w", newline="", encoding="utf-8")
    metric_file = metric_csv_path.open("w", newline="", encoding="utf-8")
    raw_file = raw_path.open("w", encoding="utf-8")
    err_file = errors_path.open("w", encoding="utf-8")
    node_writer = csv.DictWriter(node_file, fieldnames=node_fields)
    metric_writer = csv.DictWriter(metric_file, fieldnames=metric_fields)
    node_writer.writeheader()
    metric_writer.writeheader()

    start = time.monotonic()
    next_tick = start
    sample_index = 0
    successful_metric_samples = 0
    try:
        while time.monotonic() - start < a.duration:
            sample_index += 1
            now = stamp()
            try:
                nodes_obj = json.loads(kubectl("get", "nodes", "-o", "json",
                                               timeout=a.timeout).stdout)
                pods_obj = json.loads(kubectl("get", "pods", "-A", "-o", "json",
                                              timeout=a.timeout).stdout)
                node_map = {}
                for n in nodes_obj.get("items", []):
                    name = n.get("metadata", {}).get("name", "")
                    if name in a.workers:
                        node_map[name] = n
                missing = [w for w in a.workers if w not in node_map]
                if missing:
                    raise RuntimeError("worker missing from node list: " + ",".join(missing))

                requests_by_node = {
                    w: {r: 0.0 for r in RESOURCE_KEYS} for w in a.workers
                }
                pod_count = {w: 0 for w in a.workers}
                for pod in pods_obj.get("items", []):
                    spec = pod.get("spec", {})
                    w = spec.get("nodeName", "")
                    phase = pod.get("status", {}).get("phase", "Unknown")
                    if w not in requests_by_node or phase not in ("Pending", "Running"):
                        continue
                    req = pod_effective_requests(pod)
                    pod_count[w] += 1
                    for r in RESOURCE_KEYS:
                        requests_by_node[w][r] += req[r]

                sample_rows = []
                raw_nodes = {}
                for w in a.workers:
                    node = node_map[w]
                    alloc = node.get("status", {}).get("allocatable", {})
                    alloc_values = {
                        "cpu": parse_cpu(alloc.get("cpu", "0")),
                        "memory": parse_bytes(alloc.get("memory", "0")),
                        "ephemeral-storage": parse_bytes(alloc.get("ephemeral-storage", "0")),
                    }
                    raw = json.loads(kubectl(
                        "get", "--raw", f"/api/v1/nodes/{w}/proxy/stats/summary",
                        timeout=a.timeout
                    ).stdout)
                    raw_nodes[w] = raw
                    ns = raw.get("node", {})
                    cpu_nano = ns.get("cpu", {}).get("usageNanoCores")
                    mem_working = ns.get("memory", {}).get("workingSetBytes")
                    fs = ns.get("fs", {}) or {}
                    actual_cpu = float(cpu_nano) / 1e9 if cpu_nano is not None else None
                    actual_mem = float(mem_working) if mem_working is not None else None
                    fs_capacity = fs.get("capacityBytes")
                    fs_available = fs.get("availableBytes")
                    row = {
                        "timestamp_utc": now, "sample_index": sample_index,
                        "node": w,
                        "runtime": node.get("status", {}).get("nodeInfo", {}).get(
                            "containerRuntimeVersion", ""
                        ),
                        "active_pod_count": pod_count[w],
                        "alloc_cpu_cores": alloc_values["cpu"],
                        "requested_cpu_cores": requests_by_node[w]["cpu"],
                        "requested_cpu_fraction": (
                            requests_by_node[w]["cpu"] / alloc_values["cpu"]
                            if alloc_values["cpu"] else None
                        ),
                        "actual_cpu_cores": actual_cpu,
                        "actual_cpu_fraction": (
                            actual_cpu / alloc_values["cpu"]
                            if actual_cpu is not None and alloc_values["cpu"] else None
                        ),
                        "alloc_memory_bytes": alloc_values["memory"],
                        "requested_memory_bytes": requests_by_node[w]["memory"],
                        "requested_memory_fraction": (
                            requests_by_node[w]["memory"] / alloc_values["memory"]
                            if alloc_values["memory"] else None
                        ),
                        "actual_memory_working_set_bytes": actual_mem,
                        "actual_memory_fraction": (
                            actual_mem / alloc_values["memory"]
                            if actual_mem is not None and alloc_values["memory"] else None
                        ),
                        "alloc_ephemeral_bytes": alloc_values["ephemeral-storage"],
                        "requested_ephemeral_bytes": requests_by_node[w]["ephemeral-storage"],
                        "requested_ephemeral_fraction": (
                            requests_by_node[w]["ephemeral-storage"] /
                            alloc_values["ephemeral-storage"]
                            if alloc_values["ephemeral-storage"] else None
                        ),
                        "node_fs_capacity_bytes": fs_capacity,
                        "node_fs_available_bytes": fs_available,
                        "node_fs_used_fraction": (
                            1 - float(fs_available) / float(fs_capacity)
                            if fs_capacity and fs_available is not None else None
                        ),
                    }
                    node_writer.writerow(row)
                    sample_rows.append(row)
                node_file.flush()
                raw_file.write(json.dumps({
                    "timestamp_utc": now, "sample_index": sample_index,
                    "nodes": raw_nodes
                }, ensure_ascii=False) + "\n")
                raw_file.flush()

                def free_fraction(row, resource):
                    fraction = row.get("requested_" + resource + "_fraction")
                    return 1.0 - float(fraction) if fraction is not None else None

                free_cpu = [free_fraction(r, "cpu") for r in sample_rows]
                free_mem = [free_fraction(r, "memory") for r in sample_rows]
                free_disk = [free_fraction(r, "ephemeral") for r in sample_rows]
                free_cpu = [x for x in free_cpu if x is not None]
                free_mem = [x for x in free_mem if x is not None]
                free_disk = [x for x in free_disk if x is not None]
                sd_cpu, sd_mem, sd_disk = stddev(free_cpu), stddev(free_mem), stddev(free_disk)
                sds = [x for x in (sd_cpu, sd_mem, sd_disk) if x is not None]
                metric_writer.writerow({
                    "timestamp_utc": now, "sample_index": sample_index,
                    "worker_count": len(sample_rows),
                    "stddev_free_cpu_fraction": sd_cpu,
                    "stddev_free_memory_fraction": sd_mem,
                    "stddev_free_ephemeral_fraction": sd_disk,
                    "mean_residual_imbalance_proxy": sum(sds) / len(sds) if sds else None,
                    "min_free_cpu_fraction": min(free_cpu) if free_cpu else None,
                    "min_free_memory_fraction": min(free_mem) if free_mem else None,
                    "min_free_ephemeral_fraction": min(free_disk) if free_disk else None,
                })
                metric_file.flush()
                successful_metric_samples += 1
                print("[SAMPLE]", sample_index, "nodes", len(sample_rows),
                      "residual_imbalance_proxy",
                      round(sum(sds) / len(sds), 6) if sds else None, flush=True)
            except Exception as exc:
                err_file.write(json.dumps({
                    "timestamp_utc": now, "sample_index": sample_index,
                    "error": str(exc)
                }, ensure_ascii=False) + "\n")
                err_file.flush()
                print("[SAMPLE ERROR]", sample_index, str(exc), flush=True)

            next_tick += a.interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    finally:
        node_file.close()
        metric_file.close()
        raw_file.close()
        err_file.close()

    metadata = {
        "started_utc": None,
        "finished_utc": stamp(),
        "workers": a.workers,
        "interval_seconds": a.interval,
        "requested_duration_seconds": a.duration,
        "successful_metric_samples": successful_metric_samples,
        "node_snapshot_csv": str(node_csv_path),
        "imbalance_timeseries_csv": str(metric_csv_path),
        "raw_kubelet_summary_jsonl": str(raw_path),
        "errors_jsonl": str(errors_path),
        "metric_definition": (
            "For each resource dimension, free_fraction = 1 - sum(active Pod "
            "requests on node / node allocatable). Per-dimension imbalance is "
            "population standard deviation of free_fraction across workers; "
            "the aggregate proxy is the mean across CPU, memory and "
            "ephemeral-storage. Negative free fractions are retained if requests "
            "exceed allocatable. This is a residual-resource imbalance proxy, "
            "not automatically the paper's fragmentation score."
        ),
        "actual_usage_fields": (
            "Kubelet node CPU usageNanoCores, memory workingSetBytes, and node "
            "filesystem capacity/available bytes. Node filesystem is not assumed "
            "to be identical to Kubernetes allocatable ephemeral storage."
        ),
        "limitations": (
            "Pod requests approximate scheduler accounting with max(sum regular "
            "containers, max init-container request) plus overhead; advanced "
            "restartable-init sidecar semantics may require additional handling. "
            "Node-level CPU/memory usage is not itself a fragmentation metric."
        ),
    }
    write_path = root / "run_metadata.json"
    write_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("[SAVED]", root, "successful_metric_samples", successful_metric_samples,
          flush=True)
    return 0 if successful_metric_samples else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("[FATAL]", type(exc).__name__, str(exc), file=sys.stderr, flush=True)
        sys.exit(2)
