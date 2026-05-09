# FG-DSCR: Fragmentation-aware Game for Dynamic State-aware Cache Reuse

FG-DSCR is a simulation framework for edge container deployment and image-layer reuse optimization. The project studies how to jointly optimize container placement, node-side deployment order, and reusable image-layer caching under edge resource constraints.

The core idea is that container image reuse is not the final objective by itself. Layer reuse is used as a mechanism to reduce deployment cost. The final objective is the unified deployment objective defined in the paper:

\[
Obj = \alpha \cdot ACT + (1-\alpha)\cdot AMS
\]

where `ACT` is the average completion time and `AMS` is the average makespan across edge nodes. In the current experiments, we use `alpha = 0.5`.

---

## 1. Key Features

FG-DSCR contains two main stages.

### Stage I: Fragmentation-aware container placement

The first stage assigns containers to edge nodes. It considers:

- image-layer reuse potential;
- pulling cost;
- resource fragmentation;
- node load balance;
- affinity among containers sharing common layers.

The full method is named:


FG-DSCR-GC

which means:

Fragmentation-aware Game for Dynamic State-aware Cache Reuse with Greedy initialization and Cache-aware ordering
Stage II: Dynamic ordering and cache reuse

After containers are assigned to nodes, the second stage decides the execution order and reusable layer cache behavior inside each node. It supports:

dynamic state-aware ordering;
future-sharing-aware scoring;
cache replacement policies such as PGDSF / LRU / LFU;
homogeneous and heterogeneous cache configurations.
2. Cache Definition

In this project, cache size means the extra reusable image-layer cache allocated on each edge node.

It is important to distinguish it from the native disk space used by Kubernetes or container runtime systems.

Native runtime disk:
  Required by K8s/container runtime to pull and start containers.
  This part is not counted as the cache budget in this project.

Extra reusable cache:
  Additional storage used to retain image layers for future reuse.
  This is the cache size studied in the sensitivity experiments.

Therefore:

cache = 0

does not mean containers cannot run. It means no extra image-layer cache is retained. All layers can still be pulled for the current deployment, but they are not kept for future reuse.

3. Repository Structure
FG-DSCR/
├── cases/
│   ├── drtp_cache_only_sweep_88/
│   ├── drtp_cache_hetero_sweep_88/
│   ├── drtp_cache_sweep_88/
│   ├── drtp_network_env/
│   ├── drtp_scale_nodes/
│   └── drtp_resource_stress_88/
│
├── scripts/
│   ├── fg_dscr.py
│   ├── run_lrscheduler_source_baseline.py
│   ├── run_recent_layer_baselines.py
│   ├── run_gahrl_objective_greedy.py
│   ├── run_lasa_paper_reimpl.py
│   └── make_cache_hetero_sweep88_cases.py
│
├── results/
│   └── drtp/
│       └── final_exp/
│
└── README.md
4. Environment Setup

This project is CPU-only. It does not require GPU training.

conda create -n fg python=3.10 -y
conda activate fg

pip install numpy pandas matplotlib networkx

The experiments are simulation-based and do not require a real Kubernetes cluster.

5. Quick Start

Run FG-DSCR-GC on one case:

cd /home/dxaj/ssd2/jzh/FG-DSCR

python3 -u scripts/fg_dscr.py \
  --case cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_1000.json \
  --out results/drtp/final_exp/demo_fg_cache1024_1000.json \
  --beam 1 \
  --lambda-cong 1.0 \
  --lambda-frag 1.0 \
  --lambda-aff 0.2 \
  --k-pin 6 \
  --cache-policy pgdsf \
  --order-policy dynamic_state \
  --greedy-load-factor 0.9 \
  --algo-name "FG-DSCR-GC"

Check the result:

python3 - <<'PY'
import json
p="results/drtp/final_exp/demo_fg_cache1024_1000.json"
obj=json.load(open(p,"r",encoding="utf-8"))
print(json.dumps(obj["summary"], indent=2, ensure_ascii=False))
PY

Typical output fields include:

ACT
AMS
objective
downloaded_mb
reused_mb
reuse_rate
elapsed_s_internal
6. Baselines

The project currently includes the following baselines.

6.1 LRScheduler-inspired baseline
python3 scripts/run_lrscheduler_source_baseline.py \
  --case cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_1000.json \
  --out results/drtp/final_exp/demo_lrs_cache1024_1000.json
6.2 GAHRL-inspired greedy baseline
python3 scripts/run_gahrl_objective_greedy.py \
  --case cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_1000.json \
  --out results/drtp/final_exp/demo_gahrl_cache1024_1000.json
6.3 ORR-inspired recent-layer baseline
python3 scripts/run_recent_layer_baselines.py \
  --case cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_1000.json \
  --out results/drtp/final_exp/demo_orr_cache1024_1000.json \
  --algo orr
6.4 LASA-paper-reimpl baseline

This is a reproduction based on the core idea of LASA: layer-aware assignment and layer sequencing.

python3 scripts/run_lasa_paper_reimpl.py \
  --case cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_1000.json \
  --out results/drtp/final_exp/demo_lasa_cache1024_1000.json \
  --alpha-lasa 0.5 \
  --alpha-obj 0.5 \
  --no-resource-feasible \
  --algo-name "LASA-paper-reimpl"
7. Main Experiments
7.1 Overall performance under different request sizes

The overall experiment compares FG-DSCR-GC with several baselines under different request sizes.

Request sizes:

200, 300, 400, 500, 600, 700, 800, 900, 1000

Representative case setting:

catalog_size = 88
edge_nodes = 8
cache_size = 1024 MB
7.2 Stage-I placement effectiveness

This experiment evaluates the effect of resource-aware placement.

Compared methods include:

Resource-Greedy
LayerLocality-Greedy
LRScheduler-inspired
w/o soft load cap
w/o frag
FG-DSCR-GC

Main metrics:

Obj
ACT
AMS
downloaded_mb
reuse_rate
max_load
load_var
fragmentation_score

The purpose is to verify that FG-DSCR-GC reduces the final deployment objective, rather than only increasing the layer reuse ratio.

7.3 Stage-II cache sensitivity: homogeneous cache

In the homogeneous cache setting, all edge nodes have the same extra reusable cache capacity.

Cache sizes:

0, 128, 256, 384, 512, 640, 768, 896, 1024 MB

Run FG-DSCR-GC:

mkdir -p results/drtp/final_exp/cache_only_sweep88_0_1024/fg_gc

for CAP in 0 128 256 384 512 640 768 896 1024; do
  for N in 200 300 400 500 600 700 800 900 1000; do

    CASE="cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_${CAP}mb_${N}.json"
    OUT="results/drtp/final_exp/cache_only_sweep88_0_1024/fg_gc/fg_cache${CAP}_${N}.json"

    python3 -u scripts/fg_dscr.py \
      --case "$CASE" \
      --out "$OUT" \
      --beam 1 \
      --lambda-cong 1.0 \
      --lambda-frag 1.0 \
      --lambda-aff 0.2 \
      --k-pin 6 \
      --cache-policy pgdsf \
      --order-policy dynamic_state \
      --greedy-load-factor 0.9 \
      --algo-name "FG-DSCR-GC"

  done
done
7.4 Stage-II cache sensitivity: heterogeneous cache

In the heterogeneous cache setting, different edge nodes have different cache sizes, while the total cache budget is kept the same as the homogeneous setting.

The default cache ratios are:

[0.5, 0.75, 1.0, 1.25, 1.5, 0.5, 1.0, 1.5]

The average ratio is 1.0, so the total cache budget is unchanged.

Generate heterogeneous cache cases:

python3 scripts/make_cache_hetero_sweep88_cases.py

Run LASA under heterogeneous cache:

mkdir -p results/drtp/final_exp/cache_hetero_sweep88_0_1024/lasa_paper_reimpl

for CAP in 0 128 256 384 512 640 768 896 1024; do
  for N in 200 300 400 500 600 700 800 900 1000; do

    CASE="cases/drtp_cache_hetero_sweep_88/drtp_img88_cachehetero_avg${CAP}mb_${N}.json"
    OUT="results/drtp/final_exp/cache_hetero_sweep88_0_1024/lasa_paper_reimpl/lasa_paper_cache${CAP}_${N}.json"

    python3 -u scripts/run_lasa_paper_reimpl.py \
      --case "$CASE" \
      --out "$OUT" \
      --alpha-lasa 0.5 \
      --alpha-obj 0.5 \
      --no-resource-feasible \
      --algo-name "LASA-paper-reimpl"

  done
done
7.5 Scale-out experiment

The scale-out experiment evaluates performance under different numbers of edge nodes and different image catalog sizes.

Catalog sizes:

16, 50, 68, 88

Edge node numbers:

4, 8, 12, 18

Request sizes:

200, 400, 600, 800, 1000, 1200, 1500, 2000

The generated tables are:

results/drtp/final_exp/scale_plot_tables_by_edge/scale_by_edge_nodes4.md
results/drtp/final_exp/scale_plot_tables_by_edge/scale_by_edge_nodes8.md
results/drtp/final_exp/scale_plot_tables_by_edge/scale_by_edge_nodes12.md
results/drtp/final_exp/scale_plot_tables_by_edge/scale_by_edge_nodes18.md

This experiment is a scale-out experiment. The per-node configuration is fixed, so increasing the number of edge nodes also increases the total system resources.

7.6 Network environment experiment

This experiment evaluates FG-DSCR-GC under different bandwidth environments.

Scenarios:

homo_good
homo_bad
hetero_good
hetero_bad

Meaning:

homo_good:
  all edge nodes have good bandwidth.

homo_bad:
  all edge nodes have poor bandwidth.

hetero_good:
  edge nodes have heterogeneous but generally good bandwidth.

hetero_bad:
  edge nodes have heterogeneous and generally poor bandwidth.

The purpose is to verify that caching becomes more valuable when network pulling becomes more expensive.

8. Metrics
8.1 ACT

ACT is the average completion time of all containers.

ACT = average completion time over all containers
8.2 AMS

AMS is the average makespan across edge nodes.

AMS = average node-level makespan
8.3 Objective

The unified objective is:

Obj = alpha * ACT + (1 - alpha) * AMS

Current setting:

alpha = 0.5
8.4 downloaded_mb

Total downloaded image-layer volume.

8.5 reused_mb

Total reused image-layer volume.

8.6 reuse_rate

The reuse rate used in this project is a layer-byte reuse ratio:

reuse_rate = reused_mb / (reused_mb + downloaded_mb)

This is not the same as request-level cache hit rate.

A high layer-byte reuse ratio does not necessarily mean the final deployment objective is optimal.

8.7 fragmentation_score

fragmentation_score measures imbalance among normalized resource pressures, such as CPU, memory, and disk pressure.

It is used as a mechanism-level metric to analyze whether placement decisions cause resource fragmentation.

8.8 elapsed_s_internal

elapsed_s_internal is the internal running time of the algorithm script, excluding external shell overhead.

It is used to compare the computational overhead of different scheduling methods.

9. Notes on LASA-paper-reimpl

LASA-paper-reimpl is implemented as a layer-aware baseline following the core idea of layer-aware container assignment and layer sequencing.

The current implementation includes:

1. layer grouping;
2. layer-aware container-node assignment;
3. greedy layer sequencing;
4. cache-aware evaluation under the extra reusable cache definition.

In experiments, LASA usually obtains a high layer-byte reuse ratio and low downloaded volume. However, its final objective can still be worse than FG-DSCR-GC because it tends to prioritize layer locality and may cause poorer load balance or longer makespan.

This supports the main argument of FG-DSCR:

Maximizing image-layer reuse alone is not sufficient.
The final objective should jointly consider reuse benefit, pulling cost, load balance, and resource fragmentation.
10. Reproducing Figure Data
Figure 2: Stage-II homogeneous and heterogeneous cache

Homogeneous cache table:

cat results/drtp/final_exp/cache_only_sweep88_0_1024/plot_cache_only_avg_over_requests_with_lasa_paper_reimpl.md

Heterogeneous cache table:

cat results/drtp/final_exp/cache_hetero_sweep88_0_1024/plot_cache_hetero_avg_over_requests_with_lasa.md

Recommended subfigures:

(a) Homogeneous cache: average objective
(b) Homogeneous cache: layer-byte reuse ratio
(c) Heterogeneous cache: average objective
(d) Heterogeneous cache: layer-byte reuse ratio
Figure 3: Scale-out experiment
cat results/drtp/final_exp/scale_plot_tables_by_edge/scale_by_edge_nodes4.md
cat results/drtp/final_exp/scale_plot_tables_by_edge/scale_by_edge_nodes8.md
cat results/drtp/final_exp/scale_plot_tables_by_edge/scale_by_edge_nodes12.md
cat results/drtp/final_exp/scale_plot_tables_by_edge/scale_by_edge_nodes18.md

Recommended subfigures:

(a) edge_nodes = 4
(b) edge_nodes = 8
(c) edge_nodes = 12
(d) edge_nodes = 18
Figure 5: Network environment

Use the network environment tables generated under:

results/drtp/final_exp/network_env/

Recommended subfigures:

(a) homo_good
(b) homo_bad
(c) hetero_good
(d) hetero_bad
11. Running Experiments with Low Priority

The experiments are CPU-based. They do not use GPU. To avoid interfering with other processes, use:

nice -n 10 ionice -c 2 -n 7 python3 ...

Example:

nice -n 10 ionice -c 2 -n 7 \
python3 -u scripts/fg_dscr.py \
  --case cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_1000.json \
  --out results/drtp/final_exp/demo_low_priority.json \
  --beam 1 \
  --lambda-cong 1.0 \
  --lambda-frag 1.0 \
  --lambda-aff 0.2 \
  --k-pin 6 \
  --cache-policy pgdsf \
  --order-policy dynamic_state \
  --greedy-load-factor 0.9 \
  --algo-name "FG-DSCR-GC"
12. Important Experimental Interpretation
The project is simulation-based. It does not require a real K8s cluster.
K8s-native runtime storage is not counted as the reusable cache budget.
cache=0 is a valid no-cache setting.
reuse_rate is a layer-byte reuse ratio, not a request-level hit rate.
A higher reuse rate does not necessarily imply a lower final objective.
FG-DSCR-GC focuses on optimizing the unified objective by balancing:
image-layer reuse;
pulling cost;
load balance;
resource fragmentation;
dynamic cache state.
13. Citation

If this repository is used in your research, please cite the corresponding paper after publication.

@article{fgdscr,
  title   = {FG-DSCR: Fragmentation-aware Game for Dynamic State-aware Cache Reuse in Edge Container Deployment},
  author  = {Anonymous},
  journal = {Under Review},
  year    = {2026}
}
