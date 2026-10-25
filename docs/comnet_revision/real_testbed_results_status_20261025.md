# Computer Networks Major Revision: Real Testbed Results Status

**Snapshot date:** 2026-10-26  
**Code branch used for experiments:** `comnet-major-revision-2026`  
**Kubernetes cluster:** `falcons-master` + `falcons-worker1..4`  
**Purpose:** Preserve the measured real-platform evidence and clearly distinguish completed measurements from outstanding experiments.

> This file is a results summary, not a substitute for the raw artifacts. The detailed CSV/JSON/log/PCAP files are currently on the experiment server under `results/comnet_revision/k8s_real_testbed/`; they are not all uploaded to this GitHub branch by this summary commit.

## 1. Four-worker OCI workload execution

Run directory:

`results/comnet_revision/k8s_real_testbed/reviewer_core_metrics_20261025_162513/`

The real-OCI smoke run used 12 immutable OCI image references and real Kubernetes Pods. The run summary reported:

| Metric | Observed value |
|---|---:|
| Images / requests | 12 |
| Pods submitted | 12 |
| Pods succeeded | 12 / 12 |
| FALCONS planner time | 3.438 ms |
| FGP placement phase | 2.612 ms |
| DCO ordering phase | 0.551 ms |
| Controller batch wall time | 73.297 s |
| Mean request-to-finish time | 46.083 s |
| P95 request-to-finish time | 70.919 s |

Worker distribution: worker1 = 3, worker2 = 3, worker3 = 4, worker4 = 2.

**Scope limitation:** This run used real OCI images and Pods, but the initial physical cache was not verified. The runner explicitly reported `cache_model=empty_assumed` and `physical cache uncontrolled; exploratory`. Therefore this run establishes functional small-scale execution and timing observations, not controlled cold-cache performance or native PGDSF deployment.

Primary artifacts include:
- `real_oci_smoke/oci-5b0a7f79/summary.json`
- `real_oci_smoke/oci-5b0a7f79/measurements.csv`
- `real_oci_smoke/oci-5b0a7f79/events_raw.json`
- `telemetry/kubelet_summary_samples.csv`

## 2. Per-worker network transfer observation

Summary CSV:

`results/comnet_revision/k8s_real_testbed/reviewer_network_retry_20261025_163309/network_transfer_summary.csv`

| Worker | First phase TCP payload (bytes) | Repeat phase TCP payload (bytes) | Observed reduction |
|---|---:|---:|---:|
| worker1 | 29,855,981 | 16,951 | 99.9432% |
| worker2 | 29,855,980 | 16,951 | 99.9432% |
| worker3 | 29,856,011 | 16,950 | 99.9432% |
| worker4 | 3,844,174 | 16,951 | 99.5590% |

These are TCP payload bytes observed during the capture windows, not registry blob-size estimates. Worker4's first-phase payload is much smaller than those of workers1–3, so initial cache state and/or transfer conditions were not equivalent across all four nodes.

**Interpretation limit:** The first/repeat contrast is useful evidence of lower observed traffic on a repeated request. Until cache state, capture coverage, and experiment isolation are verified, do not describe it as a controlled cold-cache speedup or as proof of PGDSF eviction.

A separate worker4 proxy diagnostic observed 44 matching packets and 24,078 TCP payload bytes for an `Always` pull of an already-present image. That diagnostic is a proxy-path check, not the four-worker transfer summary.

## 3. Cached-content archive/import/unpack measurements

Run directory:

`results/comnet_revision/k8s_real_testbed/transfer_cache_metrics/run-20261025-143318-497e08/`

Target image: immutable `debian:trixie-slim` platform digest  
`docker.io/library/debian@sha256:4876fdea29cac1a33a3195f1eb1952feb1fca7dd6dd00be3e2f1879cf20d34d2`

| Worker | OCI archive export (ms) | Import with no unpack (ms) | OverlayFS unpack (ms) | Return codes |
|---|---:|---:|---:|---|
| worker1 | 143 | 444 | 618 | 0 / 0 / 0 |
| worker2 | 158 | 465 | 604 | 0 / 0 / 0 |
| worker3 | 103 | 311 | 596 | 0 / 0 / 0 |
| worker4 | 151 | 199 | 500 | 0 / 0 / 0 |

Artifacts:
- `decompression_metrics.csv`
- `decompression_metrics.json`
- `layer_inventory.csv`
- `inventory.json`

These measurements separate export, import-without-unpack, and snapshot unpack for content already available locally. They are not cold network pull times.

## 4. OCI catalog audit

Audited catalog:

`results/comnet_revision/k8s_real_testbed/oci_reviewer_fresh12_20261025_111858/catalog.json`

The audit resolved 12 public Docker Hub image references to immutable linux/amd64 manifest digests and recorded compressed layer metadata. The audit reported 36 unique layer digests and two image pairs with at least one shared digest; one reported shared pair had a displayed shared size of 0.000 MiB due to the small shared object. Catalog metadata describes registry manifests and compressed sizes; it does not establish physical cache presence on any worker.

An earlier cache inventory snapshot reported worker1 = 12/12, worker2 = 12/12, worker3 = 12/12, worker4 = 4/12 catalog digests present. This is a historical snapshot and must not be treated as the current cache state after subsequent experiments.

## 5. Registry contention first attempt (2026-10-26; preliminary, not publication-ready)

Run directories on the experiment server:

- `results/comnet_revision/k8s_real_testbed/registry_contention/run-20261026-000640-ae8311/`
- `results/comnet_revision/k8s_real_testbed/registry_contention/run-20261026-000858-49b5ce/`

### Repetition 1

| Concurrency | Pods succeeded | Batch wall time (s) | Mean successful-pull event duration (s) | Expected missing-layer bytes | Observed proxy TCP payload bytes |
|---:|---:|---:|---:|---:|---:|
| 1 | 1/1 | 11.6277 | 7.9970 | 2,211,514 | 0 |
| 2 | 2/2 | 16.8816 | 11.1675 | 29,782,552 | 0 |
| 4 | 4/4 | 73.4443 | 34.8755 | 350,149,903 | 0 |

### Repetition 2

| Concurrency | Pods succeeded | Batch wall time (s) | Mean successful-pull event duration (s) | Expected missing-layer bytes | Observed proxy TCP payload bytes |
|---:|---:|---:|---:|---:|---:|
| 1 | 1/1 | 16.8225 | 12.4630 | 17,019,999 | 0 |
| 2 | 0/2 | 7.4747 | unavailable | 41,939,439 | 0 |
| 4 | not run | — | — | — | — |

Repetition 2 stopped at concurrency 2 after `pull-2` entered `ErrImagePull`. Inspect `events_c2.json` and `pods_c2.json` in the run directory for the exact image-pull error before retrying.

**Critical validity limits:**
- The proxy TCP payload counter was zero for every level. These runs do not provide measured Registry network bytes and must not be used to claim transfer-volume reductions.
- The selected image sets and expected missing-layer sizes differed substantially across concurrency levels (for example, repetition 1 expected 2.21 MB at concurrency 1 versus 350.15 MB at concurrency 4). Raw batch times are therefore not a controlled concurrency comparison.
- Repetition 1 establishes only that the selected Pods succeeded under those run-specific conditions. Repetition 2 is incomplete because of the image-pull failure.
- The expected missing-layer bytes are catalog/cache-inventory estimates, not observed wire bytes.

The measurement script was subsequently updated on branch `comnet-major-revision-2026` to remove loopback direction filtering from tcpdump and to preserve capture diagnostics. The planner was also updated to select images within a bounded expected missing-layer size range (default 8–60 MiB, target 25 MiB). These changes require a fresh run and validation; they do not retroactively repair the zero-byte measurements above.

## 6. Physical eviction status

**Not completed.** Previous attempts to remove image metadata and wait for containerd GC did not establish that the selected content blob disappeared both from containerd content metadata and from the physical blob path. The script correctly aborted and attempted to restore the image. No result should claim successful physical PGDSF eviction.

The current helper selects a low-score candidate using a **PGDSF-score proxy** derived from catalog occurrence and stated assumptions. This is not a complete replay of an online PGDSF replacement policy, and containerd's native garbage collection is not itself PGDSF.

Do not manually unlink files under `/var/lib/containerd/io.containerd.content.v1.content/blobs/`. Continue only through containerd metadata/GC and verify both the content listing and on-disk blob before claiming eviction.

## 7. Remaining experiments and acceptance criteria

### A. Registry contention at concurrency 1, 2, and 4
- Use the same immutable image digest and a controlled, verified cache state for each trial.
- Capture proxy TCP payload bytes and record Pod pull duration/events.
- Run at least 3 repetitions per concurrency level, preferably in randomized order.
- Record success/failure, per-node pull duration, aggregate wall time, and TCP payload bytes.
- Do not compare the current first/repeat measurements as if they were a controlled cold-cache trial.

### B. Physical image-layer eviction and re-pull
- Choose a shell-capable image/layer that is safe to test and not referenced by active Pods or other image manifests.
- Record the target digest and physical file size before eviction.
- Remove the image reference using containerd commands; never directly unlink a referenced blob.
- Count eviction as successful only when the target digest is absent from `ctr -n k8s.io content ls` **and** its physical blob file is absent.
- Re-pull the immutable image, verify the blob returns, and record proxy TCP payload bytes and pull duration.
- If containerd GC retains the blob, report the attempt as unsuccessful; do not relabel image-reference removal as physical eviction.

### C. Cloud fallback
- Define the exact fallback condition in the scheduler and instrument an explicit fallback event/counter.
- Record requested placement, local-layer availability, fallback destination, reason, actual Pod node, pull duration, and traffic.
- Distinguish scheduler decisions from actual Kubernetes observations.

### D. Runtime resource fragmentation
- Keep kubelet CPU/memory samples, but do not equate node-level CPU/memory usage with resource fragmentation.
- Record per-node allocatable and requested CPU/memory/ephemeral-storage, scheduled Pod requests, actual usage, and placement failures.
- Define a reproducible fragmentation metric (for example, normalized residual-resource imbalance across CPU, memory, and disk) and calculate it from the recorded per-node state at the same timestamps.
- Report scheduler-model fragmentation separately from fragmentation measured from actual cluster state.

## 8. Evidence and reproducibility checklist

For every new run, preserve:
- immutable image catalog and digests;
- command line, code commit SHA, UTC timestamps, and random seed;
- node/containerd versions and node inventory before/after;
- raw Pod objects, events, logs, and kubelet samples;
- raw PCAP/tcpdump output and the payload-byte extraction method;
- machine-readable CSV/JSON summary;
- explicit caveats where cache state or external traffic cannot be controlled.

Never replace raw observations with values inferred only from compressed layer sizes or the scheduler's cache model.
