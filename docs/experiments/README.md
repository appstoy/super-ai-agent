# §7 实验结果（可复现）

## §7.1 消融实验

复现：`python -m tools.benchmarks.ablation --runs 5 --events 300 --work 20 --out docs/experiments/ablation.json`

合成负载：每轮 300 条事件（20 条工作请求 + 闲聊/跟进/CI），编码工作用仅消耗时间的工具代替（0.05s），重复 5 次。衡量调度与协作，而非单任务执行质量。

| 方案 | p50 完成时间 | p95 完成时间 | Router 调用均值 | prompt 字符均值 | 完成工作数 |
|---|---|---|---|---|---|
| super-agent | 0.08s | 0.10s | 246 | 1480463 | 20 |
| super-agent-no-direct | 0.10s | 0.12s | 300 | 1782050 | 20 |
| single-session-neutral | 0.53s | 0.91s | 0 | 82207 | 20 |
| single-session-delegate | 0.12s | 0.20s | 0 | 82207 | 20 |

定性结论与文章一致：Super-agent 完成最快且最稳定（p95 0.10s）；中性提示单会话最慢且长尾显著（p95 0.91s，模型几乎不委派）；提示优先委派居中（p95 0.20s）。去掉直通后完成时间基本不变，但 Router 调用从 246 增至 300（+22%），prompt 开销 +20%——直通为 Router 节省可观开销（文章报告 +30% 决定 / +37% token，同量级）。

## §7.2 Router 批处理压测

复现：`python -m tools.benchmarks.router_load --rates 1,10,20,40 --duration 25 --out docs/experiments/router-batching.json`

Router 调用延迟模拟模型（基线 2.5s，prefill 随 batch 次线性增长，指数 0.35），Worker 为 Mock；每个速率运行 25.0s。

| 到达速率 | 平均 batch | 单次请求耗时 | 单决定分摊推理时间 |
|---|---|---|---|
| 1/min | 1.00 | 2.50s | 2.50s |
| 10/min | 1.00 | 2.50s | 2.50s |
| 20/min | 1.00 | 2.50s | 2.50s |
| 40/min | 1.89 | 3.09s | 1.63s |

定性结论与文章一致：速率从 1/min 升至 40/min，平均 batch 从 1.00 升至 1.89，单次请求耗时仅从 2.50s 增至 3.09s（增长慢于 batch），单决定分摊推理时间从 2.50s 降至 1.63s——积压自动合并提高有效吞吐。

> 注：本实验基于合成负载与延迟模拟模型，绝对数值不可与文章真实模型实验直接对比；验证的是机制的定性行为。