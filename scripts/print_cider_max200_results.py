#!/usr/bin/env python3
"""Read-only CIDER RQ1-RQ4 three-seed summary, RQ5 file is precomputed.
Usage: python3 print_cider_max200_results.py
Run from ~/FG-DSCR.
"""
from collections import defaultdict
from pathlib import Path
import csv
import math
import statistics as stats

root_ptr = Path('results/LATEST_CIDER_MAX200')
ROOT = Path(root_ptr.read_text().strip()) if root_ptr.is_file() else Path('results/cider_max200_formal_20261113')
RQ1 = Path('results/cider_rq1_scale_400center_incremental_20261113_204426')
OUT = ROOT / 'paper_summary'
OUT.mkdir(parents=True, exist_ok=True)
METRICS = ['mean_ready_s', 'p95_ready_s', 'upper_mb', 'registry_mb', 'cross_domain_peer_mb', 'p2p_offload_pct']
TCRIT = {3: 4.3026527299, 8: 2.364624251, 15: 2.144786688}
seen = defaultdict(list)
daily = defaultdict(list)


def ingest(rq, setting, path, seed, day=None):
    if not path.is_file():
        print('[MISSING]', path)
        return
    with path.open(newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print('[EMPTY]', path)
        return
    for r in rows:
        method = r.get('baseline') or 'CIDER'
        for metric in METRICS:
            try:
                value = float(r[metric])
            except (KeyError, ValueError, TypeError):
                continue
            if not math.isfinite(value):
                continue
            if rq == 'RQ4':
                daily[(setting, day, method, metric)].append((seed, value))
            else:
                seen[(rq, setting, method, metric)].append((seed, value))


for n in [50, 100, 150, 200]:
    for seed in [1, 2, 3]:
        folder = RQ1 / f'seed{seed}/N{n}'
        ingest('RQ1', f'N{n}', folder / 'baselines/raw.csv', seed)
        ingest('RQ1', f'N{n}', folder / 'cider/raw.csv', seed)

for seed in [1, 2, 3]:
    for method in ['full', 'no_pulse', 'no_scout', 'no_keep', 'warmup_frequency']:
        ingest('RQ2', method, ROOT / f'rq2/seed{seed}/{method}/raw.csv', seed)

for setting_dir in sorted((ROOT / 'rq3').glob('*')):
    if not setting_dir.is_dir():
        continue
    for seed in [1, 2, 3]:
        folder = setting_dir / f'seed{seed}'
        ingest('RQ3', setting_dir.name, folder / 'baselines/raw.csv', seed)
        ingest('RQ3', setting_dir.name, folder / 'cider/raw.csv', seed)

for group in ['IBM', 'Alibaba']:
    for day_dir in sorted((ROOT / 'rq4' / group).glob('day_*')):
        for seed in [1, 2, 3]:
            folder = day_dir / f'seed{seed}'
            ingest('RQ4', group, folder / 'baselines/raw.csv', seed, day_dir.name)
            ingest('RQ4', group, folder / 'cider/raw.csv', seed, day_dir.name)

# RQ4: three seeds on the same calendar day are repeated runs, not three independent days.
daily_rows = []
for (group, day, method, metric), pairs in sorted(daily.items()):
    seeds = {s for s, _ in pairs}
    if len(seeds) != 3:
        print(f'[WARNING] RQ4 {group} {day} {method} {metric}: only {len(seeds)}/3 seeds')
    value = stats.mean(v for _, v in pairs)
    seen[('RQ4', group, method, metric)].append((day, value))
    daily_rows.append({'dataset': group, 'day': day, 'method': method,
                       'metric': metric, 'n_seeds': len(seeds), 'day_mean': value})


def write_csv(path, rows, fields):
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

write_csv(OUT / 'rq4_daily_seed_means.csv', daily_rows,
          ['dataset', 'day', 'method', 'metric', 'n_seeds', 'day_mean'])

summary = []
for (rq, setting, method, metric), pairs in sorted(seen.items()):
    values = [v for _, v in pairs]
    n = len(values)
    mean = stats.mean(values)
    sd = stats.stdev(values) if n > 1 else 0.0
    t = TCRIT.get(n)
    if t is None and n > 1:
        try:
            from scipy.stats import t as student_t
            t = float(student_t.ppf(0.975, n - 1))
        except ImportError:
            t = None
    margin = t * sd / math.sqrt(n) if t is not None else None
    expected = 15 if rq == 'RQ4' and setting == 'IBM' else 8 if rq == 'RQ4' else 3
    if n != expected:
        print(f'[WARNING] {rq} {setting} {method} {metric}: n={n}, expected {expected}')
    summary.append({'rq': rq, 'setting': setting, 'method': method,
                    'metric': metric, 'n': n, 'mean': mean, 'sd': sd,
                    'ci95_low': mean - margin if margin is not None else '',
                    'ci95_high': mean + margin if margin is not None else ''})

fields = ['rq', 'setting', 'method', 'metric', 'n', 'mean', 'sd', 'ci95_low', 'ci95_high']
write_csv(OUT / 'all_rq_summary.csv', summary, fields)
for rq in ['RQ1', 'RQ2', 'RQ3', 'RQ4']:
    write_csv(OUT / f'{rq.lower()}_summary.csv', [r for r in summary if r['rq'] == rq], fields)

by_group = defaultdict(dict)
for r in summary:
    by_group[(r['rq'], r['setting'], r['method'])][r['metric']] = r

print('\n' + '=' * 115)
print('RQ1-RQ4 RESULTS: mean ± SD ; full 95% CI in paper_summary/all_rq_summary.csv')
print('=' * 115)


def order(item):
    rq, setting, method = item
    if rq == 'RQ1':
        axis, x = 0, float(setting[1:])
    elif rq == 'RQ3':
        axisname, val = setting.split('_', 1)
        axis = {'wan': 0, 'peer': 1, 'rtt': 2}.get(axisname, 3)
        x = float(val.replace('p', '.'))
    else:
        axis, x = 0, 0
    return (rq, axis, x, setting, method != 'CIDER', method)

last = None
for key in sorted(by_group, key=order):
    rq, setting, method = key
    if (rq, setting) != last:
        print(f'\n===== {rq} | {setting} =====')
        print(f'{"Method":25} {"n":>3} {"Mean(s) ± SD":>20} {"P95(s) ± SD":>20} {"Upper(MB) ± SD":>23}')
        last = (rq, setting)
    vals = by_group[key]
    n = next(iter(vals.values()))['n']
    def fmt(metric):
        v = vals.get(metric)
        return f'{v["mean"]:.3f} ±{v["sd"]:.3f}' if v else 'N/A'
    print(f'{method[:25]:25} {n:>3} {fmt("mean_ready_s"):>20} {fmt("p95_ready_s"):>20} {fmt("upper_mb"):>23}')

print('\n===== RQ5 CONTROL RUNTIME =====')
file5 = ROOT / 'rq5/runtime_summary.csv'
if file5.is_file():
    with file5.open(newline='', encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            print(f'N={r["nodes"]:>3} {r["module"]:25} n={r["n"]} '
                  f'mean={float(r["mean_ms"]):.3f}ms SD={float(r["sd_ms"]):.3f}ms '
                  f'CI95=[{r["ci95_low"]}, {r["ci95_high"]}]')
else:
    print('[MISSING]', file5)

print('\nSaved:', OUT)
for filename in ['all_rq_summary.csv', 'rq1_summary.csv', 'rq2_summary.csv',
                 'rq3_summary.csv', 'rq4_summary.csv', 'rq4_daily_seed_means.csv']:
    print('  ', OUT / filename)
