# Computer Networks major revision — experimental worklist

Manuscript: COMNET-D-26-04832; due 2026-11-08.
Base: master SHA 06c56badfd52fe82b1acc09e2776b36d714a38c1. Revision branch: comnet-major-revision-2026.
Do not cherry-pick SECON code, force-push, or overwrite historical results.

## Ordered by implementation difficulty (not by reviewer number)

1. Editorial/theoretical corrections (R2.7): clean Chinese labels, broken spaces, duplicated sentences; qualify potential-game Nash and five-round bounded refinement.
2. Reproducibility audit (R2.3): catalog and request sources, anonymized image mapping, seeds, capacities, bandwidths, calibration and normalization, code SHAs. Audit what is verifiable; do not invent trace provenance.
3. Raw metric export (R2.2): all-request mean, P95, cloud fallback fraction, fragmentation, downloaded MB, objective components. Recompute from per-request logs where available; never confuse edge-only ACT with all-request mean.
4. Bounded-refinement convergence (R2.7): potential versus actual accepted iteration, stops by round cap/no improvement, 1/3/5/10/20 rounds; report local neighborhood stopping only.
5. Measured scheduling overhead (R1.2/R2.6): Phase-I, Phase-II, full controller time, actual wall-clock, cold/warm runs; include N=200..1000, M up to 18; compare saved latency on same unit and hardware.
6. Cache policy and module ablations (R2.5): FGP-only, DCO-only, full, w/o PGDSF; LRU/LFU/GDSF/PGDSF under shared cache semantics.
7. Parameter robustness (R1.4/R2.2): lambda1..4, eta1..4, beta/rho/kappa, objective weights and reference values. Hold the test set out from tuning.
8. Repeated trials and statistical testing (R1.4/R2.4): at least 20 independently sampled test workloads, paired common seeds across baselines, 95% confidence intervals and matched difference tests.
9. Baseline adaptation and training parity (R1.3/R2.5): distinguish GAHRL-inspired greedy from independently trained GAHRL-like code; specify hyperparameter and budget, fair shared evaluator, open scripts.
10. Cost-benefit + non-monotonicity (R1.5): delay component vs CA/fragmentation, overhead in total latency, per-request case studies, Pareto frontier.
11. Concurrent Kubernetes event model (R2.1): revise execution/occupation semantics for serial startup vs concurrent runtime and resource release, with sensitivity under both models; independent model-change experiment.
12. Small real Kubernetes prototype (R1.1/R2.6): 3-4 workers with same runtime + local registry, warm/cold layer pulls, overlapping downloads, registry bottleneck, extraction, kubelet and control-plane overhead; calibrate sim-to-real.

## Critical controls

- Main manuscript Eq. (8) J = Dnorm + CAnorm + Fragnorm, not legacy 0.5 ACT + 0.5 AMS.
- Main manuscript D = .5 all-request mean + .5 P95, including cloud fallback.
- Raw manuscript normalization refs Dref=440.7696, CAref=.77924, Fragref=.180069 are calibration choices, NOT universal scales.
- Manuscript FGP weights = (1,.1,.2,.2), DCO gains=(1,.15,50,.02), PGDSF=(2,.2,.5), beam=1, rounds=5; benchmark must verify implementation parity.
- A thresholded five-round process does not establish a Nash equilibrium.
- Avoid data leakage by choosing scales/parameters only on calibration workloads.
- Physical startup and measured transfer latency must never be presented as simulator-only data.
- Do not silently replace the paper's serial completion model with concurrent K8s model without retesting all compared methods.

## First run

```bash
git fetch origin
git switch comnet-major-revision-2026
python3 -m py_compile tools/comnet_revision/measure_scheduler_runtime.py
python3 tools/comnet_revision/measure_scheduler_runtime.py \
  --case cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_200.json \
  --repeats 10 --warmup 1 --beam 1 --rounds 5 \
  --neighbor-mode move_swap
```

WARNING: The currently available code is a researcher-written trace-driven simulator; benchmark figures collected here represent algorithm runtime only. They do not validate live K8s processing or network overhead.
