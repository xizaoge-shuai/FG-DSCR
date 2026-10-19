# FALCONS 官方镜像压力实验说明

这组实验是独立新增的真实 Kubernetes 验证，不覆盖之前的轻量 Custom OCI 实验。当前物理 containerd 未采用 PGDSF，所以不能把真实实验的表现写作真实 PGDSF 缓存替换收益。

## 1. 在 Master 建立独立 worktree

    cd ~/FG-DSCR
    git fetch origin comnet-major-revision-2026
    git worktree add --detach ~/FG-DSCR-oci-pressure origin/comnet-major-revision-2026
    cd ~/FG-DSCR-oci-pressure
    python3 -m py_compile tools/comnet_revision/k8s_real_oci_smoke.py tools/comnet_revision/run_k8s_official_pressure_suite.py tools/comnet_revision/analyze_k8s_official_pressure_stages.py

## 2. 获取 12 个 Docker Hub 官方镜像的真实 digest 和 layer 元数据

    python3 -u tools/comnet_revision/audit_oci_image_layers.py \
      --images python:3.11-slim-bookworm python:3.12-slim-bookworm python:3.13-slim-bookworm \
        golang:1.26-bookworm golang:1.26-alpine golang:1.27-alpine \
        node:20-bookworm-slim node:22-bookworm-slim node:24-bookworm-slim \
        redis:8.6-alpine redis:8.8-alpine redis:8-alpine \
      --out-dir results/comnet_revision/k8s_real_testbed/oci_official_12

本命令不会下载镜像层 blob，只读 Docker Hub manifests。代理可选：--proxy http://127.0.0.1:17897。必须验证 catalog.json 12 镜像且 0 errors，镜像均已 pin Linux/amd64 digest；检查 shared_layers.csv 的实际共用层。

## 3. 先做 Pilot

    python3 tools/comnet_revision/k8s_real_cluster_audit.py
    python3 -u tools/comnet_revision/run_k8s_official_pressure_suite.py \
      --stage pilot --repeats 1 --with-telemetry --timeout 2400

每任务 CPU request 0.40/0.45/0.50 core；RAM request 0.50/0.625/0.75 GiB；实际执行 16/20/24 s 的 shell CPU loop 和内存 tmpfs 192 MiB。不能只看资源 requests；需要 kubelet CPU/memory samples 证据。

## 4. 同输入四种方法比较

    python3 -u tools/comnet_revision/run_k8s_official_pressure_suite.py \
      --stage placement --repeats 3 --with-telemetry --timeout 2400

    python3 -u tools/comnet_revision/run_k8s_official_pressure_suite.py \
      --stage ordering --repeats 3 --with-telemetry --timeout 2400

placement: N=12，全局并发 12，主要观察资源约束下的放置与任务完成。ordering: N=12，全局并发 3，主要观察 FALCONS 对同放置 FGP+FCFS 的 DCO 顺序收益。主要论文 baseline ILR-SA 也在同一组 OCI case 上运行；K8s Default 是原生调度器参考。所有方法按固定相同 seed 和全部 12 镜像运行，顺序随机化。若中断可传 --resume-dir 至具体 suite-* 目录恢复。

## 5. 阶段性分析

    python3 tools/comnet_revision/analyze_k8s_real_suites.py \
      --suite-root results/comnet_revision/k8s_real_testbed/official_pressure_suite \
      --catalog results/comnet_revision/k8s_real_testbed/oci_official_12/catalog.json \
      --out-dir results/comnet_revision/k8s_real_testbed/official_pressure_analysis

    python3 tools/comnet_revision/summarize_k8s_real_telemetry.py \
      --suite-root results/comnet_revision/k8s_real_testbed/official_pressure_suite \
      --out-dir results/comnet_revision/k8s_real_testbed/official_pressure_telemetry

    for d in results/comnet_revision/k8s_real_testbed/official_pressure_suite/suite-*; do
      python3 tools/comnet_revision/analyze_k8s_official_pressure_stages.py --suite-dir "$d"
      cat "$d/stage_breakdown_report.md"
    done

阶段包括 FGP 放置、DCO 排序、ILR-SA 放置与排序、API ack、Pod create-to-start、Pulling/Pulled、ACT/P95/D、真实 CPU/内存采样。Frag_static 仅是基于 requests 的静态投影，非实时动态碎片。Pulling Event 及宿主机 RX/TX 均不能解释为实际镜像下载字节。

## 6. 尚未解决的真实缓存问题

所有镜像即便大于模型缓存容量，containerd 的实际 image store 不会因为 FALCONS 模拟 1024 MB 就自动执行 PGDSF。若导师要求 PGDSF 的实测收益，必须另外实现独立缓存环境下的可执行预算、digest 驱逐/保留日志和 Registry 传输计量。不能在共享工作节点上清空全量镜像，也不能把模拟 evicted 字节冒充为物理下载减少。已有结果不涉及物理 PGDSF 优势。
