# OnlineNorm valid24 summary

| method | cases | missing | avg_obj | obj_vs_FG_base | avg_reuse | reuse_gain | avg_downloaded | download_reduction | avg_ACT | avg_AMS |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| FG-base | 24 | 0 | 1239.026 | 0.00% | 0.392596 | +0.000000 | 187967.000 | 0.00% | 877.019 | 1601.032 |
| OnlineNorm-trial2 | 24 | 0 | 1243.132 | -0.33% | 0.385353 | -0.007243 | 188360.500 | -0.21% | 860.735 | 1625.528 |
| OnlineNorm-trial12 | 24 | 0 | 1238.298 | 0.06% | 0.375437 | -0.017159 | 191010.958 | -1.62% | 846.864 | 1629.732 |

Conclusion: OnlineNorm reduces ACT but worsens AMS, reuse_rate, and downloaded_mb. The best valid24 objective improvement is only 0.06%, so OnlineNorm is not used as the main configuration.
