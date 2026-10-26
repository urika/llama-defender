# 引擎换代对照终裁：optiq-35B vs 27B-mlx（2026-10-27）

> **结论**：预注册触发条件**两条全中** → 维持 **optiq-35B 为主引擎**；切换已于 2026-10-27 执行
> （`active.conf → splash-qwen36-35b-optiq.conf`，代理 profile `splash-qwen36-35b-optiq`）。
> 27B-mlx conf 保留为「能力档」备件（`splash-qwen38-27b-mlx.conf`）。

## 一、预注册触发条件与读数

| 触发条件（P3 中报预注册） | 27B-mlx 读数 | 判定 |
|---|---|---|
| ① 终局 resolve < 40% | northstar-repro 批（27B 生产栈）主臂 **raw 33.3%（21/63）**；剔 infra 35.6%（21/59）/ 净率 36.2%（21/58）[CI 25.1,49.1]；对照臂 30.2%/30.6% | **命中**（raw 口径 <40%；两口径均低于历史基线 46%，但任务集不同不可直比） |
| ② 墙钟无优势 | 单轮墙钟 p50 **7.4s** / p90 14.5s（27B，4,672 请求窗）；optiq 单轮 ≈**3.5s** → 27B 慢 **2.1×** | **命中**（无优势） |

数据源：`swe-eval/results/experiments/exp-20261024-p0-northstar-repro/judgment-20261026.md`（**已签署定稿 2026-10-26**，双判读位交叉核验一致）；吞吐面为本仓引擎 `/metrics` 窗口聚合。

## 二、可测面对照（终值）

| 指标 | optiq-35B（主引擎） | 27B-mlx（能力档） |
|------|------|------|
| decode | **116–132 tok/s**（两口径：10-21 窗口 132 / 10-27 长会话复测 116） | 47.5 tok/s |
| prefill | 1,101–1,154 tok/s | — |
| TTFT p50 | 2.2s（10-21）/ 0.87s（10-27 复测） | 4.65s |
| 单轮墙钟 p50 | ≈3.5s | 7.4s（p90 14.5s） |
| state 命中 | 94.6% | 98.4% |
| reasoning | thinking on + low+2048 | none |
| **知识质量门 v3** | **PASS 58/62 = 93.5%**（2026-10-27 首测，唯一全类别过线模型） | 未测（轮换回 optiq 的既成事实下不再补测；如再启用需先过门） |

## 三、判定说明与边界

1. **为什么不是「27B 质量更好所以留 27B」**：northstar 批测的是 27B 生产栈的**路由臂**（prod_default vs local_only），
   不是 27B-vs-optiq 的模型对照；且该批 33.3% 的结果与历史 46% 不同任务集，不可直比。模型层证据只有吞吐面
   与知识质量门——两者 optiq 全部占优或打平。
2. **27B 的保留理由**：作为「能力档」备件（dense 27B 在长程一致性上的潜在优势未否定，但当前无数据支持其收益
   超过 2.1× 墙钟代价）。其 conf 已移植 idle-release 120m（9454bf5），随下次启用自然生效。
3. **批中重启污染已由判读件登记**：northstar 批 10-26 12:17 probe_fail 停批 → 12:25 resume，8 死 run 不在分母；
   conf 曾临时回退 idle-release 120m→10m「回到批次起飞版本」——段界与处置见判读件 §二。
4. **9B 共存结论不变**：主引擎（27B 19GB 或 optiq 33GB）与 9B（5.7GB）同驻均触发全局换页
   （`docs/04-analysis-diagnostics/ornith-9b-memory-pressure-20261020.md`），不因换代改变。

## 四、产物卫生观察（转 swe-eval 侧核）

`judgment-20261026-stats.json`（10-26 20:46）的 E1 读数（主臂 raw **24/63=38.1%**、net 24/62=38.7%）与
**已签署 md 的 21/63=33.3%/35.6%** 不一致；双判读位交叉核验采信 md 口径，json 疑为另口径/陈旧重算产物。
建议 swe-eval 侧核一次产物一致性（不影响本终裁：即便取 json 口径 38.1%，仍 <40% 触发条件①，判定不变）。

## 五、后续

- 主引擎 optiq 质量基线已建档（`docs/06-reference-metrics/BENCHMARK.md` 质量门 v3 跨模型表）
- 下一批如再评估 27B：先跑质量门 v3（62 条，~5 分钟）再进批，避免「未测质量即换模」
- 引擎换代流程教训：换模必跑 `capability_audit.py`（ADR-017 §2），并复核 `PROXY_CTX_EPOCH_TRIGGER_TOKENS`
  等跨代不变式（epoch 阈值证据链 `epoch-trigger-threshold-drift-20261027.md`）
