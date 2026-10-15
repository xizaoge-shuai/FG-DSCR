# FALCONS: small-scale real Kubernetes validation for COMNET revision

## Purpose
Use nine-scale simulations for large-scale effectiveness and 20-seed robustness, and a small real Kubernetes platform for deployment feasibility, actual image transfer, Pod startup/finish, runtime overhead and simulation-to-real trends. Follow the reduced real-world validation strategy from EdgeBus, Internet of Things 28 (2024) 101368, Sections 5.1.3 and 5.4.5: eight GKE nodes, 20 users, 30 time steps, a subset of simulation baselines.

## Recommended first testbed
Start with one control-plane node and four distinct workers. Prefer spare physical hosts; alternatively use distinct KVM virtual machines on an existing Linux host, or five cloud VMs on the same VPC. Explicitly disclose virtual shared host CPU, storage, network, and contention; these are not independent physical edge devices. Scale worker cores/memory according to the preflight. Avoid installing cluster services on a shared production host without administrator approval.

## Workload and images
- Select 3-5 real OCI images from the original Docker Hub catalog and verify immutable image digests, platform architectures, exact shared layer digests and compressed sizes.
- Reproduce shared-layer patterns with a documented sample of 20/40 container requests, map simulation CPU/memory/runtime into actual Job requests/limits and workload program. State all remappings; synthetic runtime is not true application execution unless a real workload process is used.
- Use a dedicated measured OCI registry or registry logging proxy, record transmitted bytes and image pull errors. Do not infer downloaded bytes solely from Pod timestamps.
- Cold cache trials: isolated worker VM snapshot reset (preferred); deleting Pods does not clear containerd cache. Warm trials: reuse retained layers. NEVER bulk prune container images on a shared host.
- Explicitly document containerd runtime and that its built-in image/content GC is not the Python PGDSF algorithm. Testing PGDSF eviction on Kubernetes requires a separate real adapter; otherwise label this only as placement and ordering validation.

## Comparable strategies and controls
1. Offline placement/order track: precompute per-algorithm placement and order (FALCONS; ILR-SA; FALCONS no-DCO) on identical input; launch equal Jobs following per-node orders, explicitly bind nodes with nodeName. This bypasses kube-scheduler, so describe as external orchestration control, not as native scheduler plugin.
2. Native Kubernetes control: use default kube-scheduler under same available images/nodes and resource requests. It may exhibit different queue semantics and is a complementary baseline, not fully paired with offline batches.
3. First serialize starts per node to approximate old model; then add concurrent waves with a controlled per-node in-flight cap to test realistic resource release.
4. Five repeated trials per setting where feasible; randomize method order and use comparable cold-reset procedures.

## Measurements
- Python algorithm controller runtime, Kubernetes API apply/acknowledgment, Pod creation-to-start, Pulling/Pulled events, container startedAt/finishedAt, Pod completion, actual image bytes from registry, CPU/mem/pod-failure and eviction events.
- Trial-level mean/median/P95/worst-case ready/finished delay, cold/warm improvements, event-based pull time, node heterogeneity impact, total experiment cost, failures.
- Simulation-vs-real correlations and MAE on matched workload subsets. The article's 300-second simulated cloud provisioning is NOT a physical measurement or comparable empirical composite J.
- If full PGDSF control isn't actually implemented in containerd, do not claim a real-testbed demonstration of the proposed cache-replacement policy.

## Procedure
A. Read-only host preflight and select node topology.
B. Provision isolated K8s/K3s cluster and OCI registry with fixed manifests and image digests.
C. Verify two-image shared layer smoke test, one cold and one warm run.
D. Execute 20/40 Job runs across policies, trials and cold/warm conditions.
E. Save raw Pod JSON, events, registry log, job configurations, node runtime metadata, timestamps and failures, then draw real-testbed figures.

## Limitations
Small VM deployment proves API/execution feasibility and observed trends, not real geographic edge-network heterogeneity. Physical cache eviction control is a separate implementation task. Running time on a shared VM host will not reproduce independent physical worker bandwidth.
