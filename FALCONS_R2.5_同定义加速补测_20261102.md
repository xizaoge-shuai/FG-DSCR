# R2.5：保持输出不变的计算加速补测

日期：2026-11-02。这是对已完成792次缓存模块实验的工程追加，全部为yz主机CPU仿真。没有重写或替换原792次的计时、原始结果、训练选型或确认统计。

结论：B的计算成本可以降低，但这两项加速尚未使其控制器快于LRU；不能据此宣称已经救回对标准策略的统一净延迟优势。

## 配置与计时口径

仅使用原训练seed 20261101501、20261101502，N=1000、每节点cache=1024模型MB、8个模型节点、S1历史配置；每方法每seed重复5次，轮换顺序交错计时，共30次。两颗训练seed不是新的确认组，5次技术重复不能当作5个独立工作负载。

S1的λ=(1,10,0,.03)、resource_asc、greedy_load_factor=.8、FGP Rbr5，DCO beam1、α=(1,.15,50,.02)；缓存β=2、ρ=.2、κ=.5、u=50。与792次的S1对应，不把它描述成论文默认权重。输入、节点资源、带宽、空缓存与对应训练case相同。

|CPU中位耗时|原B|加速B|完整LRU|
|---|---:|---:|---:|
|完整scheduler.run，含模型执行replay|1803.761ms|1482.307ms|1376.982ms|
|控制器FGP+DCO，不含执行replay|1736.743ms|1439.452ms|1340.687ms|

完整scheduler CPU降低17.82%，但仍比LRU高7.65%；控制器FGP+DCO仍比LRU高7.37%。表中数字是每方法10次观测的合并中位数，不能与其他阶段的中位数跨表配对；分seed结果和全部wall/process-CPU见CSV。

## 改了什么，以及如何保证定义不变

1. 同一次候选评分中，复用同一个remaining集合的未来层计数。剖析发现原B约2492次候选转移却计算future_counts约4968次；复用保留Counter插入顺序，仅限该次评分。
2. 只把原u50 DP递推编译为C++，保留single-first、离散κ、候选顺序、严格比较和tie、Python set复制顺序、H、soft pins、hard trim、时钟与DCO增益。没有换成exact-MB目标。

B本来就是“容量可行保护＋本批无未来需求层降优先级”的新增缓存机制。这里的“定义不变”指加速前后B相同，也检查了原PGDSF的native-only路径；它不把B变回原稿PGDSF。

编译环境：yz Linux、g++9.4.0，-O3 -std=c++11 -fPIC -shared -fno-fast-math -ffp-contract=off。Linux .so在yz上验证，不能直接在Windows本机加载。这是针对PGDSF DP的实现加速；LRU仍用原标准实现，不能把语言实现差异说成算法复杂度优势。

## 输出与证据检查

- Memo在全部36个训练case/profile组中，完整输出、诊断及所有非计时指标完全一致。
- Native共72个完整比较通过：36组B与memo+native B，及36组原PGDSF与native-only原PGDSF；队列、放置、缓存、下载和目标均一致。
- 2000个淘汰案例的集合和set迭代顺序一致，其中875个实际执行native DP。
- 30次交错计时均检查完整输出SHA；独立审计还在yz复跑了代表性完整流程及两项回归测试。

未改评价权重、J归一化、方法参数或已选候选；训练结果仍落后LRU，因此没有据此宣称新确认优势，也没有反复改确认seed。小缓存压力下的驻留不足及FGP稿件/实现差异仍未解决。

## 文件

- [792次完整流程结果与参数](C:/Users/jiazonghan/Desktop/查找论文文献/k8s/FALCONS_R2.5_完整流程缓存替换与定义核查_20261101.md)
- [完整工程评估](C:/Users/jiazonghan/Desktop/查找论文文献/k8s/ccfa-workfiles/experiments/r25-full-pipeline-20261101/speed_assessment_20261102/ASSESSMENT.md)
- [逐次计时CSV](C:/Users/jiazonghan/Desktop/查找论文文献/k8s/ccfa-workfiles/experiments/r25-full-pipeline-20261101/speed_assessment_20261102/native_train_timings.csv)
- [完整一致性检查](C:/Users/jiazonghan/Desktop/查找论文文献/k8s/ccfa-workfiles/experiments/r25-full-pipeline-20261101/speed_assessment_20261102/native_train_parity.json)
- [源码与结果SHA清单](C:/Users/jiazonghan/Desktop/查找论文文献/k8s/ccfa-workfiles/experiments/r25-full-pipeline-20261101/speed_assessment_20261102/artifact_manifest.json)

工程补测的独立审计另见实验目录内SPEED_AUDIT.md；主AUDIT.md仅覆盖原792次冻结证据。
