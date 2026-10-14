#!/usr/bin/env python3
"""Summarize local CN revision results; never report legacy Obj as paper J."""
import csv, json, pathlib, statistics
from collections import defaultdict
R=pathlib.Path('results/comnet_revision')
def records(p, header=True):
    if not p.exists():
        print('MISSING:',p); return []
    with p.open(encoding='utf-8',newline='') as f:
        return list(csv.DictReader(f)) if header else list(csv.reader(f))

print('\n=== R1.2/R2.6: Incremental FALCONS controller (phase-only ms) ===')
for p in sorted((R/'runtime_main').glob('*_runtime.csv')):
    rows=records(p)
    if not rows: continue
    n=int(rows[0]['requests']);m=int(rows[0]['nodes'])
    f=lambda k: statistics.mean(float(x[k]) for x in rows)
    sd=statistics.stdev(float(x['controller_ms']) for x in rows) if len(rows)>1 else 0
    print(f'N={n:4d} M={m:2d} reps={len(rows):2d} FGP={f("placement_ms"):9.2f} DCO={f("ordering_ms"):8.2f} Total={f("controller_ms"):9.2f} SD={sd:7.2f}')

print('\n=== Baseline FULL PROCESS times (not directly comparable with controller phase-only) ===')
for p in sorted((R/'baseline_runtime'/'raw').glob('*_time.csv')):
    rows=records(p,header=False)
    grp=defaultdict(list)
    for x in rows:
        if len(x)!=7: continue
        try: grp[int(x[1])].append(float(x[3]))
        except ValueError: pass
    for n in sorted(grp):
        x=grp[n]; sd=statistics.stdev(x) if len(x)>1 else float('nan')
        print(f'{p.stem:<17} N={n:<4} reps={len(x):2d} process_wall_mean={statistics.mean(x):9.3f}s SD={sd:8.3f}s')
    if sum(len(x) for x in grp.values())!=90:
        print('  WARNING: expected 90 valid rows for this baseline!')

print('\n=== R2.7: Bounded FGP refinement (NOT a Nash proof) ===')
rows=records(R/'convergence'/'convergence_summary.csv')
if rows:
    bycap=defaultdict(list)
    for x in rows:bycap[int(x['round_cap'])].append(x)
    for cap,rr in sorted(bycap.items()):
        avg=lambda col:statistics.mean(float(x[col]) for x in rr)
        noimp=sum(x['stop_label'].startswith('no_improve') for x in rr)
        print(f'cap={cap:2d} scales={len(rr)} accepted={avg("accepted_ops"):6.2f} potential_drop={100*avg("relative_potential_drop"):7.4f}% FGP={avg("phase1_wall_ms"):8.2f}ms no_improve={noimp}/{len(rr)}')
    if 5 in bycap and 20 in bycap:
        indexed={c:{int(x['requests']):x for x in rr} for c,rr in bycap.items()}
        common=sorted(set(indexed[5])&set(indexed[20]))
        diffs=[(float(indexed[5][n]['final_potential'])-float(indexed[20][n]['final_potential']))/max(abs(float(indexed[5][n]['final_potential'])),1e-12)*100 for n in common]
        print('5->20 potential additional improvement (% of phi at cap=5):',round(statistics.mean(diffs),5))

print('\n=== R2.5: Existing CLI ablations (EDGE metrics ONLY; NOT paper J) ===')
patterns={
    'Full':'ablation/full',
    'noFGPrefine':'ablation/no_fgp_refinement',
    'noDCO':'ablation/no_dco',
    'noFuture':'ablation/no_future_share',
    'PGDSF':'cache_policy/pgdsf',
    'LRU-proxy':'cache_policy/lru',
    'LFU-proxy':'cache_policy/lfu',
}
for name,rel in patterns.items():
    folder=R/rel; vals=[]
    for n in range(200,1001,100):
        p=folder/f'req{n}.json'
        if not p.exists():continue
        try:
            x=json.loads(p.read_text(encoding='utf-8'));s=x['summary']
            vals.append((n,float(s['ACT']),float(s['downloaded_mb']),
                         float(s.get('fail_rate',s.get('ca_rate',0))),float(s.get('reuse_rate',0))))
        except (ValueError,KeyError,TypeError) as e:print(' WARNING',p,e)
    if len(vals)<9:print(f' MISSING {name}: {len(vals)}/9 in {folder}')
    if vals:
        print(f'{name:<13} n={len(vals)} edge_ACT={statistics.mean(v[1] for v in vals):9.2f}s edge_DL={statistics.mean(v[2] for v in vals):10.1f}MB fallback={statistics.mean(v[3] for v in vals):7.3f} reuse={statistics.mean(v[4] for v in vals):7.3f}')
print('\nCAUTION: Final manuscript J uses ALL-request mean/P95 and fixed paper reference values. Do not substitute legacy objective.')
