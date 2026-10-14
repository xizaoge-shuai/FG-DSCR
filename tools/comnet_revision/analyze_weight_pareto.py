#!/usr/bin/env python3
"""Formal R2.2: fixed-manuscript J weight/reference sensitivity + Pareto.

Uses 54 verified article-result records, never legacy ACT/AMS objective.
No new independent trial is implied by reweighting already recorded results.
"""
import argparse,csv,json,statistics
from pathlib import Path
BASE={'D':440.7696,'CA':0.77924,'Frag':0.180069}
METHOD='FG-DSCR-GC'
def pareto(rows,dims):
    return [a for a in rows if not any(b is not a and all(b[d]<=a[d]+1e-12 for d in dims) and any(b[d]<a[d]-1e-12 for d in dims) for b in rows)]
def score(row,w,refs):
    return w[0]*row['D']/refs['D'] + w[1]*row['CA']/refs['CA'] + w[2]*row['Frag']/refs['Frag']
def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source',default='results/comnet_revision/raw_metrics/raw_allrequest_by_method_scale.csv')
    p.add_argument('--out-dir',default='results/comnet_revision/weight_pareto')
    a=p.parse_args()
    with open(a.source,newline='',encoding='utf-8') as f:data=list(csv.DictReader(f))
    rows=[]
    for r in data:
        n=int(float(r['requests']))
        obj={'requests':n,'method':r['method'],
             'D':.5*(float(r['ACT_all'])+float(r['P95_all'])),
             'CA':float(r['CA_rate']),'Frag':float(r['frag_score']),
             'DownloadedMB':float(r['downloaded_mb'])}
        if abs(obj['D']-float(r['Delay_all']))>.003:raise ValueError('Delay formula mismatch')
        rows.append(obj)
    scales=sorted(set(r['requests'] for r in rows))
    methods=sorted(set(r['method'] for r in rows))
    if len(rows)!=54 or len(scales)!=9 or len(methods)!=6 or METHOD not in methods:
        raise ValueError('Need 54 source records: 9 paired workloads x 6 methods')
    byscale={n:{r['method']:r for r in rows if r['requests']==n} for n in scales}
    if any(len(byscale[n])!=len(methods) for n in scales):raise ValueError('Missing workload-method')
    avg=[]
    for method in methods:
        rr=[r for r in rows if r['method']==method]
        avg.append({'method':method,**{k:statistics.mean(r[k] for r in rr) for k in ('D','CA','Frag','DownloadedMB')}})
    front3=pareto(avg,('D','CA','Frag'))
    front4=pareto(avg,('D','CA','Frag','DownloadedMB'))
    configs=[('equal',1.,1.,1.)]
    for k in range(3):
        for v in (.5,2.):
            w=[1.]*3;w[k]=v;w=[3.*x/sum(w) for x in w]
            configs.append((['delay','cloud','fragmentation'][k]+f'_{v:g}',*w))
    configs += [('delay_prioritized',1.8,.6,.6),('placement_prioritized',.6,1.2,1.2)]
    profiles=[(name,(wD,wCA,wFrag),BASE) for name,wD,wCA,wFrag in configs]
    for key in BASE:
        for factor in (.8,1.2):
            refs=BASE.copy();refs[key]*=factor
            profiles.append((f'{key}_ref_x{factor:.1f}',(1,1,1),refs))
    results=[];per_scale=[]
    for name,w,refs in profiles:
        means={r['method']:score(r,w,refs) for r in avg}
        ordered=sorted(methods,key=lambda m:(means[m],m))
        for n in scales:
            curr={m:score(r,w,refs) for m,r in byscale[n].items()}
            winner=min(curr,key=lambda m:(curr[m],m))
            per_scale.append({'profile':name,'requests':n,'winner':winner,'FALCONS_J':curr[METHOD],
                              'ILRSA_J':curr['ILR-SA'],'FALCONS_best':int(winner==METHOD)})
        fg=means[METHOD];il=means['ILR-SA']
        results.append({'profile':name,'wD':w[0],'wCA':w[1],'wFrag':w[2],
                        'D_ref':refs['D'],'CA_ref':refs['CA'],'Frag_ref':refs['Frag'],
                        'winner':ordered[0],'FALCONS_rank':ordered.index(METHOD)+1,
                        'FALCONS_mean_J':fg,'ILRSA_mean_J':il,
                        'FALCONS_relative_improvement_vs_ILRSA_pct':100*(il-fg)/il,
                        'FALCONS_wins_by_scale':sum(x['FALCONS_best'] for x in per_scale if x['profile']==name)})
    out=Path(a.out_dir);out.mkdir(parents=True,exist_ok=True)
    def save(name,rs):
        with (out/name).open('w',newline='',encoding='utf-8') as f:
            wr=csv.DictWriter(f,fieldnames=list(rs[0]));wr.writeheader();wr.writerows(rs)
    save('weight_and_reference_sensitivity.csv',results)
    save('profile_by_request.csv',per_scale)
    save('mean_raw_objective_components.csv',avg)
    save('pareto_3d.csv',front3)
    save('pareto_4d_with_network_cost.csv',front4)
    (out/'analysis_provenance.json').write_text(json.dumps({
        'source':a.source,'records':len(rows),'scales':scales,
        'fixed_article_refs':BASE,'weight_variation':'hold sum(w)=3',
        'reference_variation':'one reference +/-20%, no test-set recalibration',
        'limitations':['Reweights fixed existing simulations; no repeated workload samples or CIs',
                       'Pareto is across methods on mean D, CA, fragmentation',
                       'Network-volume Pareto is supplementary, not part of J',
                       'Changes weights after scheduling; no new policy optimization']
    },indent=2),encoding='utf-8')
    print('R2.2: formal J reweighting of 54 article records')
    print('Pareto frontier D,CA,Frag:',[x['method'] for x in front3])
    print('Pareto frontier D,CA,Frag,DL:',[x['method'] for x in front4])
    for r in results:
        print(f"{r['profile']:<29} winner={r['winner']:<14} FG_rank={r['FALCONS_rank']} "
              f"FG_J={r['FALCONS_mean_J']:.3f} ILR_J={r['ILRSA_mean_J']:.3f} "
              f"relative_improvement={r['FALCONS_relative_improvement_vs_ILRSA_pct']:.2f}% "
              f"FG_wins={r['FALCONS_wins_by_scale']}/9")
    print('Saved',out)
if __name__=='__main__':main()
