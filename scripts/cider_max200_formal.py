#!/usr/bin/env python3
"""CIDER SECON formal suite, at most 200 nodes; resume existing states/results.
Run from ~/FG-DSCR with conda fg active.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

PROJ = Path.cwd().resolve()
sys.path.insert(0, str(PROJ))
PYTHON = sys.executable
ENV = os.environ.copy()
ENV['PYTHONHASHSEED'] = '0'
ENV['PYTHONPATH'] = str(PROJ) + (os.pathsep + ENV['PYTHONPATH'] if ENV.get('PYTHONPATH') else '')

CASE_ROOT = PROJ / 'cases/cider_rq_scale_400center_20261113_181217'
RQ1_ROOT = PROJ / 'results/cider_rq1_scale_400center_incremental_20261113_204426'
ROOT = PROJ / 'results/cider_max200_formal_20261113'
NAME40 = 'cider_nodes40_domains8_req800_weak'
BASELINES = ['registry', 'dragonfly_style', 'peersync_style', 'metapipe_reactive_style', 'ilrsa_style']
SHORT_BASELINES = ['registry', 'peersync_style', 'ilrsa_style']
SCALES = [50, 100, 150, 200]
SEEDS = [1, 2, 3]


def log(*xs):
    print(*xs, flush=True)


def readcsv(path: Path):
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open(newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def complete(path: Path, methods=None):
    try:
        rows = readcsv(path)
    except Exception:
        return False
    if methods is None:
        return len(rows) == 1 and bool(rows[0].get('mean_ready_s'))
    return len(rows) == len(methods) and {x.get('baseline') for x in rows} == set(methods)


def run(cmd, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    log('[RUN]', ' '.join(map(str, cmd)), '\n     LOG:', output)
    with output.open('w', encoding='utf-8') as f:
        result = subprocess.run(list(map(str, cmd)), stdout=f, stderr=subprocess.STDOUT,
                                cwd=PROJ, env=ENV, check=False)
    if result.returncode != 0:
        lines = output.read_text(encoding='utf-8', errors='replace').splitlines()
        for line in lines[-30:]:
            log(line)
        raise RuntimeError(f'Exit {result.returncode}: {output}')


def case_path(n: int, seed: int) -> Path:
    return CASE_ROOT / f'seed{seed}/weak_scale_arrival/cider_nodes{n}_domains{n//5}_req{n*20}_weak.json'


def state_file(case: Path, seed: int, bout: Path) -> Path:
    return bout / 'states' / f'{case.stem}__seed{seed}.json'


def net(wan=250, peer=12.5, factor=1.0):
    return ['--wan', str(wan), '--core-capacity', '1000', '--domain-capacity', '300',
            '--access-capacity', '200', '--peer-upload', str(peer),
            '--registry-latency-ms', str(40*factor),
            '--inter-domain-latency-ms', str(10*factor),
            '--intra-domain-latency-ms', str(2*factor)]


def cider_args(case, seed, bout, out, netargs, ablation='full'):
    return [PYTHON, '-u', '-m', 'secon_exp.experiments.run_cider_mainline',
            '--state-root', bout, '--cases', case, '--seed', seed,
            *netargs, '--pulse-budget-mb', '2048', '--pulse-max-transfers', '0',
            '--pulse-refill-ratio', '0.50', '--scout-alpha', '1', '--scout-beta', '4',
            '--scout-gamma', '0.5', '--scout-source-concurrency', '0',
            '--keep-v', '3e6', '--keep-upper-budget-mb-s', '150',
            '--ablation', ablation, '--out', out]


def busy_baseline(case, bout):
    result = subprocess.run(['pgrep', '-af', 'secon_exp.experiments.run_scale_baselines_v2'],
                            capture_output=True, text=True, check=False)
    token_case, token_out = f'--cases {case.relative_to(PROJ)}', f'--out {bout.relative_to(PROJ)}'
    return any(token_case in s and token_out in s for s in result.stdout.splitlines())


def baseline(case, seed, bout, methods, netargs, reuse_state=None):
    bout.mkdir(parents=True, exist_ok=True)
    state = state_file(case, seed, bout)
    if reuse_state and not state.is_file():
        state.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(reuse_state, state)
        log('[REUSE FG STATE]', state)
    if complete(bout / 'raw.csv', methods) and state.is_file():
        log('[SKIP BASELINES]', case.stem, 'seed', seed)
        return
    if not state.exists() and busy_baseline(case, bout):
        log('[REUSE IN-PROGRESS FG] Existing N200 seed run continues; no duplicate started')
        deadline = time.monotonic() + 3*3600
        while busy_baseline(case, bout):
            if time.monotonic() >= deadline:
                raise RuntimeError(f'Existing FG exceeded waiting limit; preserved: {bout}')
            time.sleep(60)
    if complete(bout / 'raw.csv', methods) and state.is_file():
        return
    run([PYTHON, '-u', '-m', 'secon_exp.experiments.run_scale_baselines_v2',
         '--cases', case, '--seeds', seed, '--warmup-fraction', '0.5',
         *netargs, '--baselines', *methods, '--resume', '--out', bout], bout / 'run.log')
    if not complete(bout / 'raw.csv', methods) or not state.is_file():
        raise RuntimeError(f'Incomplete baseline or FG state: {bout}')


def cider(case, seed, bout, out, netargs, ablation='full', warmup_lfu=False):
    out.mkdir(parents=True, exist_ok=True)
    if complete(out / 'raw.csv'):
        log('[SKIP CIDER]', out)
        return
    if (out / 'raw.csv').exists():
        (out / 'raw.csv').rename(out / 'raw.incomplete.csv')
    command = cider_args(case, seed, bout, out, netargs, ablation)
    if warmup_lfu:
        command = [PYTHON, '-u', '-m', 'scripts.review_mechanisms',
                   '--scout-mode', 'audit', '--cache-policy', 'warmup_lfu',
                   '--diagnostics', out / 'review_diag.json', '--', *command[4:]]
        # Forward only run_cider_mainline options, excluding python -u -m <module>.
    run(command, out / 'run.log')
    if not complete(out / 'raw.csv'):
        raise RuntimeError(f'Incomplete CIDER output: {out}')


def prepare():
    """Only add missing 50/150-node inputs. Preserve all earlier cases."""
    from secon_exp.network.expand_case import expand_case_nodes
    for seed in SEEDS:
        parent = CASE_ROOT / f'base/seed{seed}/cider_base_40000.json'
        fallback = case_path(200, seed)
        source = parent if parent.is_file() else fallback
        if not source.is_file():
            raise FileNotFoundError(f'Missing both parent and fallback: {parent} / {fallback}')
        base = json.loads(source.read_text(encoding='utf-8'))
        target_dir = CASE_ROOT / f'seed{seed}/weak_scale_nodes_max200'
        target_dir.mkdir(parents=True, exist_ok=True)
        todo = []
        for n in [50, 150]:
            dst = case_path(n, seed)
            if dst.exists() and dst.stat().st_size:
                log('[SKIP CASE]', dst)
                continue
            subset = json.loads(json.dumps(base))
            subset['containers'] = subset['containers'][:n*20]
            subset = expand_case_nodes(subset, num_nodes=n, num_domains=n//5, seed=seed)
            subset.setdefault('meta', {}).update({'num_nodes': n, 'num_domains': n//5,
                                                  'num_containers': n*20})
            subset.setdefault('metadata', {})['scale'] = {'nodes': n, 'domains': n//5,
                                                          'requests': n*20, 'seed': seed,
                                                          'source': str(source)}
            generated = target_dir / dst.name
            generated.write_text(json.dumps(subset, ensure_ascii=False, indent=2), encoding='utf-8')
            todo.append(dst)
        if todo:
            run([PYTHON, '-u', 'scripts/add_drtp_arrivals.py',
                 '--input-dir', target_dir,
                 '--output-dir', CASE_ROOT / f'seed{seed}/weak_scale_arrival',
                 '--arrival-csv', 'data/stats/drtp_stats/minute_arrivals.csv',
                 '--window-minutes', '1440', '--horizon-s', '240', '--seed', seed],
                ROOT / 'logs' / f'build_cases_seed{seed}.log')
        for n in SCALES:
            case = case_path(n, seed)
            if not case.is_file():
                raise FileNotFoundError(case)
            obj = json.loads(case.read_text(encoding='utf-8'))
            if len(obj['nodes']) != n or len(obj['containers']) != n*20:
                raise RuntimeError(f'Unexpected workload size: {case}')
            log('[CASE]', f'N={n}', f'seed={seed}', case)
    (ROOT / 'CASES_READY').write_text(time.strftime('%Y-%m-%dT%H:%M:%S'))


def rq1():
    # Do not compete with an already-running FG placement from the original RQ1.
    check = subprocess.run(['pgrep', '-af', 'secon_exp.experiments.run_scale_baselines_v2'],
                           capture_output=True, text=True, check=False)
    if any('cider_rq1_scale_400center_incremental_20261113_204426' in line
           for line in check.stdout.splitlines()):
        log('[WAIT] Existing RQ1 FG is still computing. Reuse its results rather than duplicate it.')
        deadline = time.monotonic() + 3 * 3600
        while True:
            check = subprocess.run(['pgrep', '-af', 'secon_exp.experiments.run_scale_baselines_v2'],
                                   capture_output=True, text=True, check=False)
            if not any('cider_rq1_scale_400center_incremental_20261113_204426' in line
                       for line in check.stdout.splitlines()):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('An existing original-RQ1 FG is still running after 3h; not duplicated.')
            time.sleep(60)
    for n in SCALES:
        for seed in SEEDS:
            case = case_path(n, seed)
            bout = RQ1_ROOT / f'seed{seed}/N{n}/baselines'
            cout = RQ1_ROOT / f'seed{seed}/N{n}/cider'
            log('\n== RQ1 ==', f'N={n}', f'seed={seed}')
            baseline(case, seed, bout, BASELINES, net())
            cider(case, seed, bout, cout, net())
    (ROOT / 'RQ1_DONE').touch()


def rq2():
    for seed in SEEDS:
        n = 200
        case = case_path(n, seed)
        bout = RQ1_ROOT / f'seed{seed}/N200/baselines'
        if not state_file(case, seed, bout).is_file():
            raise RuntimeError(f'RQ1 FG state missing: {bout}')
        for ablation in ['full', 'no_pulse', 'no_scout', 'no_keep']:
            out = ROOT / f'rq2/seed{seed}/{ablation}'
            if ablation == 'full':
                original = RQ1_ROOT / f'seed{seed}/N200/cider/raw.csv'
                if not complete(original):
                    raise RuntimeError(f'RQ1 full missing: {original}')
                out.mkdir(parents=True, exist_ok=True)
                if not complete(out / 'raw.csv'):
                    shutil.copy2(original, out / 'raw.csv')
                    (out / 'REUSED_FROM.txt').write_text(str(original))
                continue
            log('\n== RQ2 ==', ablation, f'seed={seed}')
            cider(case, seed, bout, out, net(), ablation)
        log('\n== RQ2 ==', 'warmup_frequency', f'seed={seed}')
        cider(case, seed, bout, ROOT / f'rq2/seed{seed}/warmup_frequency',
              net(), warmup_lfu=True)
    (ROOT / 'RQ2_DONE').touch()


def rq3():
    sweeps = []
    for wan in [125, 187.5, 250, 312.5, 375]:
        sweeps.append((f'wan_{str(wan).replace(".", "p")}', net(wan=wan)))
    for peer in [12.5, 18.75, 25, 31.25, 37.5]:
        sweeps.append((f'peer_{str(peer).replace(".", "p")}', net(peer=peer)))
    for factor in [0.5, 0.75, 1, 1.25, 1.5]:
        sweeps.append((f'rtt_{str(factor).replace(".", "p")}', net(factor=factor)))
    for tag, config in sweeps:
        for seed in SEEDS:
            case = case_path(200, seed)
            shared = RQ1_ROOT / f'seed{seed}/N200/baselines'
            source_state = state_file(case, seed, shared)
            if not source_state.is_file():
                raise RuntimeError(f'RQ1 FG state missing: {source_state}')
            bout = ROOT / f'rq3/{tag}/seed{seed}/baselines'
            cout = ROOT / f'rq3/{tag}/seed{seed}/cider'
            log('\n== RQ3 ==', tag, f'seed={seed}')
            baseline(case, seed, bout, SHORT_BASELINES, config, reuse_state=source_state)
            cider(case, seed, bout, cout, config)
    (ROOT / 'RQ3_DONE').touch()


def production_cases():
    ibm = sorted((PROJ / 'cases/cider_review77_20261109/test').glob(f'day_*/arrival/{NAME40}.json'))
    ali = sorted((PROJ / 'cases/cider_alibaba_robust_20261111_134205').glob(f'day_*/{NAME40}.json'))
    if len(ibm) != 15 or len(ali) != 8:
        raise RuntimeError(f'Production inputs missing: IBM={len(ibm)}, Alibaba={len(ali)}')
    return [('IBM', p.parent.parent.name, p) for p in ibm] + \
           [('Alibaba', p.parent.name, p) for p in ali]


def old_prod_state(group, day, seed):
    if seed != 1:
        return None
    if group == 'IBM':
        root = PROJ / 'results/cider_review77_20261109/test'
    else:
        ptr = PROJ / 'results/LATEST_CIDER_ALIBABA_RUN'
        root = Path(ptr.read_text().strip()) if ptr.is_file() else PROJ / 'results/cider_alibaba_robust_20261111_134205'
    p = root / day / 'baselines' / 'states' / f'{NAME40}__seed1.json'
    return p if p.is_file() else None


def rq4():
    for group, day, case in production_cases():
        obj = json.loads(case.read_text(encoding='utf-8'))
        if len(obj['nodes']) != 40 or len(obj['containers']) != 800:
            raise RuntimeError(f'Unexpected production case size: {case}')
        for seed in SEEDS:
            bout = ROOT / f'rq4/{group}/{day}/seed{seed}/baselines'
            cout = ROOT / f'rq4/{group}/{day}/seed{seed}/cider'
            earlier = old_prod_state(group, day, seed)
            if earlier:
                # Reuse old FG state only when basic case content matches.
                st = json.loads(earlier.read_text(encoding='utf-8'))
                target = st.get('target_case', {})
                if len(target.get('nodes', [])) != 40 or \
                   target.get('layer_sizes_mb') != obj.get('layer_sizes_mb'):
                    earlier = None
                else:
                    from secon_exp.adapters.fg_dscr_adapter import FGDSCRAdapter
                    _, expected = FGDSCRAdapter().split_windows(obj, warmup_fraction=0.5, seed=1)
                    if target.get('containers') != expected.get('containers'):
                        earlier = None
            log('\n== RQ4 ==', group, day, f'seed={seed}')
            baseline(case, seed, bout, SHORT_BASELINES, net(), reuse_state=earlier)
            cider(case, seed, bout, cout, net())
    (ROOT / 'RQ4_DONE').touch()


def rq5():
    rows = []
    for n in SCALES:
        for seed in SEEDS:
            path = RQ1_ROOT / f'seed{seed}/N{n}/cider/raw.csv'
            data = readcsv(path)
            if len(data) != 1:
                log('[PENDING RQ5]', path)
                continue
            r = data[0]
            row = {'nodes': n, 'seed': seed}
            for k in ['pulse_ms', 'scout_ms', 'keep_ms', 'scheduler_runtime_ms']:
                if k in r and r[k] not in ('', None):
                    row[k] = float(r[k])
            rows.append(row)
    dest = ROOT / 'rq5'
    dest.mkdir(parents=True, exist_ok=True)
    fields = ['nodes', 'seed', 'pulse_ms', 'scout_ms', 'keep_ms', 'scheduler_runtime_ms']
    with (dest / 'runtime_runs.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(rows)
    stats = []
    for n in SCALES:
        for k in fields[2:]:
            xs = [float(r[k]) for r in rows if r['nodes'] == n and k in r]
            if not xs:
                continue
            mean = statistics.mean(xs)
            sd = statistics.stdev(xs) if len(xs) > 1 else 0.0
            ci = 4.30265273 * sd / (len(xs)**0.5) if len(xs) == 3 else None
            stats.append({'nodes': n, 'module': k, 'n': len(xs), 'mean_ms': mean,
                          'sd_ms': sd, 'ci95_low': mean-ci if ci is not None else '',
                          'ci95_high': mean+ci if ci is not None else ''})
    with (dest / 'runtime_summary.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['nodes', 'module', 'n', 'mean_ms', 'sd_ms', 'ci95_low', 'ci95_high'])
        w.writeheader(); w.writerows(stats)
    log('[RQ5]', len(rows), 'runs summarized:', dest)
    if len(rows) == 12:
        (ROOT / 'RQ5_DONE').touch()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('stage', choices=['prepare', 'rq1', 'rq2', 'rq3', 'rq4', 'rq5', 'all'])
    args = ap.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    ptr = PROJ / 'results/LATEST_CIDER_MAX200'
    ptr.write_text(str(ROOT.relative_to(PROJ)) + '\n')
    log('[ROOT]', ROOT)
    log('[FG]', 'incremental; shared placement and warm cache')
    if args.stage in ['all', 'prepare', 'rq1']:
        prepare()
    if args.stage in ['all', 'rq1']:
        rq1()
    if args.stage in ['all', 'rq2']:
        rq2()
    if args.stage in ['all', 'rq5']:
        rq5()
    if args.stage in ['all', 'rq3']:
        rq3()
    if args.stage in ['all', 'rq4']:
        rq4()
    log('[DONE]', args.stage)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log('[FAILED]', type(e).__name__, str(e))
        raise
