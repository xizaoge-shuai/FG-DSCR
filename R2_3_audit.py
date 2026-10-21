#!/usr/bin/env python3
"""FALCONS R2.3: read-only provenance/trace/case/calibration audit, stdlib-only.
Usage: python3 R2_3_数据溯源核查.py --repo ~/FG-DSCR
No network, no case modification; never invent missing original trace provenance.
"""
import argparse,collections,datetime,hashlib,json,math,pathlib,statistics,sys

def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        while True:
            b=f.read(1<<20)
            if not b:break
            h.update(b)
    return h.hexdigest()
def put(lines,s=''):lines.append(s)
def fmt(v):return f'{v:.6g}' if isinstance(v,float) else str(v)
def lookup(root,patterns):
    results=[]
    for pat in patterns:
        results.extend(root.glob(pat))
    return sorted(set(p for p in results if p.is_file()))
def load(p):return json.loads(p.read_text(encoding='utf-8'))
def get_rank_freq(case):
    req=case.get('containers') or []
    # The original exported case may omit image_ref; group by full ordered layer digest sequence.
    labels=[]
    for c in req:
        value=c.get('image_ref') or c.get('image') or c.get('repo')
        if not value:value='layers:'+hashlib.sha256(json.dumps(c.get('layers',[]),sort_keys=True).encode()).hexdigest()[:12]
        labels.append(str(value))
    counter=collections.Counter(labels)
    ranks=sorted(counter.values(),reverse=True)
    n=len(req)
    resources={q:[float(c.get('resources',{}).get(q,0)) for c in req] for q in ('cpu','mem','disk')}
    durations=[float(c.get('run_time',0)) for c in req]
    return {'n':n,'distinct_image_signatures':len(counter),'top1_pct':100*ranks[0]/n if n else None,
            'top5_pct':100*sum(ranks[:5])/n if n else None,
            'hhi':sum((x/n)**2 for x in ranks) if n else None,
            'resource_ranges':{q:[min(x),max(x)] if x else [] for q,x in resources.items()},
            'runtime_range':[min(durations),max(durations)] if durations else [],
            'exact_request_image_names_present':all(('image_ref' in c or 'image' in c or 'repo' in c) for c in req)}
def main():
    p=argparse.ArgumentParser()
    p.add_argument('--repo',default='.')
    p.add_argument('--out',default='results/comnet_revision/reproducibility_r23/source_audit.md')
    a=p.parse_args();root=pathlib.Path(a.repo).expanduser().resolve()
    if not root.exists():p.error('repo root missing '+str(root))
    out=pathlib.Path(a.out);out=out if out.is_absolute() else root/out
    lines=['# R2.3 原始数据与校准溯源核查（只读自动报告）','',
        '说明：本报告扫描现有文件，不会将生成程序的默认参数当成历史主实验事实，也不会将缺失数据替换为推断。',
        '',f'运行目录：`{root}`','',
        '## 一、原始轨迹与处理后输入']
    profiles=lookup(root,['data/**/*drtp*profile*.json','data/**/*trace*profile*.json','data/**/drtp_profile.json'])
    if profiles:
        for pp in profiles[:20]:
            try:
                obj=load(pp);kw=obj.get('repo_weights',[]) if isinstance(obj,dict) else []
                put(lines,f'- DRTP profile: `{pp.relative_to(root)}`，SHA-256=`{sha(pp)}`，repo_weights={len(kw)}')
                put(lines,f'  - profile 顶层字段：`{list(obj) if isinstance(obj,dict) else type(obj).__name__}`')
                if isinstance(obj,dict):
                    put(lines,'  - 数据来源字段（如有）：`'+str({k:v for k,v in obj.items() if any(z in k.lower() for z in ('source','trace','date','seed','url','zone','filter','path'))})[:1400]+'`')
            except Exception as e:put(lines,f'- 无法读取 `{pp}`: {e}')
    else:put(lines,'- **未发现 drtp_profile.json 或其他 trace profile 文件。尚不能确认原始轨迹文件、可用区、过滤条件、时间范围。**')
    tracefiles=lookup(root,['data/**/*.log.gz','data/**/*.csv.gz','data/**/*drtp*trace*','data/**/*registry*trace*'])
    if tracefiles:
        put(lines,'- 候选原始轨迹文件（路径与哈希需和实际 profile 生成脚本进一步核对）：')
        for x in tracefiles[:20]:put(lines,f'  - `{x.relative_to(root)}`，文件大小={x.stat().st_size} 字节')
    else:put(lines,'- 没有在 data/ 中发现符合常见命名的原始轨迹文件。')
    catalogs=lookup(root,['data/catalog/*catalog*.json','data/catalog/*image*list*.txt','data/catalog/*image*list*.json'])
    if catalogs:
        for path in catalogs[:35]:
            try:
                if path.suffix=='.json':
                    obj=load(path);n=len(obj) if isinstance(obj,list) else len(obj.get('images',[])) if isinstance(obj,dict) else '?'
                else:n=len([x for x in path.read_text().splitlines() if x.strip()])
            except Exception as e:n='ERROR '+str(e)
            put(lines,f'- 镜像目录 `{path.relative_to(root)}`：条目数={n}；SHA-256=`{sha(path)}`')
    else:put(lines,'- **未发现可追溯 Docker Hub 镜像目录；请在原始实验机器查找是否只有不受版本管理的数据。**')
    put(lines,'\n## 二、历史主实验输入文件及分布')
    groups={}
    for key,pat in [('prefix','cases/drtp_cache_only_sweep_88_prefix/drtp_img88_cacheonly_1024mb_*.json'),
                    ('nonprefix','cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_*.json')]:
        files=sorted(root.glob(pat));groups[key]=[]
        if not files:put(lines,f'- `{key}`：**未找到主实验输入目录下的 JSON**。')
        for path in files:
            try:
                obj=load(path);d=get_rank_freq(obj)
                cids=[{'layers':c.get('layers'),'resources':c.get('resources'),'run_time':c.get('run_time')} for c in obj.get('containers',[])]
                groups[key].append((d['n'],path,cids))
                put(lines,f'- `{path.relative_to(root)}`：n={d["n"]}，不同镜像层签名={d["distinct_image_signatures"]}，top1={fmt(d["top1_pct"])}%，top5={fmt(d["top5_pct"])}%，HHI={fmt(d["hhi"])}；任务时长={d["runtime_range"]}；CPU/内存/磁盘需求范围={d["resource_ranges"]}；SHA-256=`{sha(path)}`。')
                meta=obj.get('meta',{})
                put(lines,f'  - case.meta: `{meta}`')
                if not d['exact_request_image_names_present']:
                    put(lines,'  - **case 缺少逐请求的 image_ref：可按层组合区分镜像，但需要单独的 catalog 才能恢复名称映射。**')
            except Exception as e:put(lines,f'- `{path}` 读取失败：{e}')
    for name,items in groups.items():
        if len(items)<2:continue
        maxlen,maxpath,base=max(items,key=lambda x:x[0])
        for n,path,contents in items:
            if n==maxlen:continue
            prefix_ok=(contents==base[:n])
            put(lines,f'- `{name}`：{n} 请求与 {maxlen} 请求前缀完全一致 = **{prefix_ok}**')
    put(lines,'\n## 三、校准文件与实际归一化实现')
    put(lines,'- **原始 J 参考值的历史来源已明确：** `results/drtp/k8s_same_scale/tables_allrequest_fig5_edgeca300_prefix/fig5_allrequest_detail.md`，由六种方法 × 九个规模的 54 条原始记录计算平均值；这不是独立、留出的 calibration workload。')
    detail=root/'results/drtp/k8s_same_scale/tables_allrequest_fig5_edgeca300_prefix/fig5_allrequest_detail.md'
    if detail.exists():
        hdr=None;records=[]
        for line in detail.read_text(encoding='utf-8',errors='replace').splitlines():
            if not line.startswith('|'):continue
            cols=[x.strip() for x in line.strip().strip('|').split('|')]
            if cols and cols[0]=='requests' and all(k in cols for k in ('Delay_all','CA_rate','frag_score')):
                hdr=cols;continue
            if hdr and len(cols)==len(hdr) and cols[0].isdigit():
                row=dict(zip(hdr,cols))
                try:records.append({k:float(row[k]) for k in ('Delay_all','CA_rate','frag_score')})
                except ValueError:continue
        put(lines,f'- 主实验评价明细：`{detail.relative_to(root)}`，有效记录={len(records)}，SHA-256=`{sha(detail)}`')
        if len(records)==54:
            vals={k:statistics.mean(x[k] for x in records) for k in ('Delay_all','CA_rate','frag_score')}
            put(lines,f'- 独立重新计算：`D_ref={vals["Delay_all"]:.16g}`，`CA_ref={vals["CA_rate"]:.16g}`，`Frag_ref={vals["frag_score"]:.16g}`')
            want=(440.7696111111112,.7792425925925928,.1800691666666666)
            got=(vals['Delay_all'],vals['CA_rate'],vals['frag_score'])
            put(lines,f'- 与历史未四舍五入参考值一致：{all(abs(x-y)<1e-6 for x,y in zip(got,want))}')
        else:put(lines,'- **有效行数不是 54，不能宣称已复算校准常数；检查表格版本和表头。**')
    else:put(lines,'- **历史 54 行评价明细尚未在当前机器定位到：必须从原实验机器复制该文件，才能核验三个固定参考值。**')
    put(lines,'- `scripts/fg_dscr.py` 的 FGP 归一化依赖**当前候选放置**产生的分项统计；这不是固定校准工作负载拟合的四个常数。')
    put(lines,'- `tools/summarize_fig5_allrequest_latency.py` 会在当前评价集合上计算均值归一化；多种子脚本 `run_paired_multiseed.py` 另行采用论文固定参考值评价 `J`。这两种口径不可混用。')
    for pat in ['results/comnet_revision/**/*calibrat*','results/drtp/k8s_same_scale/**/*calibrat*','data/**/*calibration*.json','data/**/*calibration*.csv','results/comnet_revision/**/*norm*ref*.json']:
        hits=lookup(root,[pat]);
        if hits:
            put(lines,f'- 校准文件候选 `{pat}`：')
            for x in hits[:20]:put(lines,f'  - `{x.relative_to(root)}`，SHA-256=`{sha(x)}`')
    put(lines,'\n## 四、历史主实验参数记录')
    envfiles=lookup(root,['results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env','results/drtp/k8s_same_scale/sweep/best_fg_params.env'])
    for path in envfiles:
        put(lines,f'- `{path.relative_to(root)}` SHA-256=`{sha(path)}`：')
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip().startswith('export FG_') or line.strip().startswith('export LAMBDA_CA='):
                put(lines,'  - `'+line.strip()+'`')
    if not envfiles:put(lines,'- **缺少 best_fg_edgeca_params.env：需要回溯原始运行时设置。**')
    put(lines,'\n## 五、必须继续定位的原始证据')
    for msg in ['原始 DRTP 文件路径、数据版本、可用区及采集时间范围',
                '从匿名 Registry 请求生成 repo_weights 的具体脚本、过滤条件和统计粒度',
                '原始 Docker Hub 镜像列表、具体 tag/manifest digest、抓取时间',
                '主实验 prefix 目录的实际生成器及 PRNG 种子与输入哈希',
                '确认 J 参考值取自主评价 54 条记录，而非独立 calibration set；决定是否另做留出校准',
                'FGP 论文固定 μ 参数与代码动态尺度的差异处理方式']:
        put(lines,'- [ ] '+msg)
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('Saved',out,'profiles',len(profiles),'catalogs',len(catalogs),'prefixcases',len(groups.get('prefix',[])))
    return 0
if __name__=='__main__':sys.exit(main())
