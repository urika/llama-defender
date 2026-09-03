# 受控失忆实验协议（exp-1-amnesia）

> **状态**：就绪待执行｜**日期**：2026-08-30｜**前置**：配置指纹已入产（sessions.jsonl 每轮自带 `config`/`conf_hash`）
> **关联**：PRD R9/R10（v3.1 批次 2 效度门禁）· IFC 设计 §5 Phase 2 · 台账 `logs/diag/experiments.jsonl`

---

## 1. 假设（预注册，不允许事后更换）

**H1**：真实截断（ILE）导致信念熵恶化并降低任务完成率。三环分解：

- **E1 机制环**：传感装置可信——处理组 ILE 事件 ≥30；manifest 覆盖率 100%（每个被截断单元恰有一条索引行）。**E1 失败 = R10.1 缺陷，修复优先于一切。**
- **E2 信号环（主判定）**：ILE 后 2 轮内 H_BE 上升幅度与损失量相关，**Spearman |ρ|≥0.4**，或"ILE 后 5 轮内失败"预测 **AUC≥0.7**。达标 → R9.3 门控解锁（Phase 3）；不达标 → 门控永不上线，IFC 止步度量（PRD 既定）。
- **E3 结局环（次要）**：处理组 resolved 率较对照 73% 下降 ≥15pp（配对面板"对照过/处理挂"占优）。不降 = text 任务族信息冗余高（本身是发现），E2 独立成立。
- **E4 探索**：处理组熵 down 趋势占比 < 对照组。

**双无信号的预先裁决**：E2/E3 均无 → 只允许"信号无效 / 该族不敏感"两种解释，裁决方式为 code 任务族补批，禁止事后换指标。

## 2. 基线（2026-08-30 健康批，进场前冻结）

| 维度 | 值 |
|---|---|
| 结局率 | resolved 73%（30 任务配对面板） |
| 信息损失 | ILE = 0（manifest 零落盘；档案重判 53 view_reset / 0 ile / 266 clean） |
| 熵-结局相关 | 无分离（干净样本 down 11/4 vs up 2/1） |
| 熵分布 | p25=0.609 / p50=0.756 / p75=0.968 bits（伪迹 v2 过滤后 282 条） |
| D_ledger | 双峰：85% 全忆 + 15% 全忘（答案风格混杂，本实验只作参考轴） |

## 3. 设计

队列配对对比（非同时 A/B——配置全局，无自动分流）：同一 syn-text 30 任务、同模型同配置，唯一变量为截断阈值；对照组 = 上述基线批，处理组 `--force` 重跑。**队列识别三层**：时间窗（台账）→ config 指纹（权威，`config.keep_messages==12`）→ 任务配对（runs.jsonl instance_id + session 前缀 join）。

## 4. 进场参数（三处，全部 reloadable——只 reload，不 restart）

```bash
# configs/active.conf
export PROXY_CTX_KEEP_MESSAGES=12      # 40→12: 唯一处理变量
export PROXY_HBE_MIN_CHARS=8000        # 20000→8000: 视图压小后探针不至全体跳过
export PROXY_HBE_SAMPLE_EVERY=2        # 4→2: ILE 邻接采样加密(批跑延迟不敏感)
```

## 5. 操作流程

```
0 准备   ① 指纹已入产 ✅  ② 台账工具就绪 ✅  ③ 本协议冻结 ✅
         ④ 试点: 三参数临时生效, 跑 2 个任务 --force
            验收: manifest 落盘 + ile=true + 探针 ok + 无崩溃 → 恢复参数
1 进场   确认批跑独占窗口(不开其他会话)
         改三参数 → ./manage.sh reload → 日志确认 RELOAD OK
         python3 tools/experiment_ledger.py begin --id exp-1-amnesia \
           --hypothesis "ILE→熵恶化→结局下降(E1机制/E2信号主判/E3结局)" \
           --treatment keep_messages=12,hbe_min_chars=8000,hbe_sample_every=2 \
           --batch "syn-text-30 --force" \
           --protocol docs/03-experiments-testing/amnesia-experiment-protocol-20260830.md
2 批跑   swe-eval 30 任务 --force(route_forced=local 已内建)
         中止条件: 崩溃率异常 / 看门狗频繁重启 / OOM → 立即恢复参数出场
3 出场   恢复三参数 → reload → experiment_ledger.py end --id exp-1-amnesia --conclusion <报告路径>
4 分析   当天: trace_query ifc --verdicts ~/APP/swe-eval/results/runs.jsonl \
             --cohort keep_messages=12 --limit 200
         报告 → ../04-analysis-diagnostics/amnesia-experiment-report-20260830.md
         台账写结论指针; 按 E2 判定更新 PRD R9.3 状态
```

## 6. 污染识别与排除

窗口内会话无 runs.jsonl 配对 → contamination，退出结局分析；`view_reset` 轮不计 ILE；`route_target!=local` 过滤；verdict=None 退出结局分析但保留于信号分析；探针 `<tool_call>` 伪迹（任意位置含）默认过滤、`--raw-hbe` 可重放。

## 7. 口径与可比性注意

- **世代**：处理组为 hbe 新世代（completion_budget=160，finish=stop），对照为旧世代（48-token 截断 98%）——H 基线漂移，跨世代对比按 `completion_budget` 分组或以组内趋势为准；E2 的 ILE 前后对比是**组内**度量，不受世代混杂。
- **样本量**：预计 ILE 事件 20-40 个/批；\|ρ\|≥0.4 门槛判定按可用 n 报告效应量与置信区间，≥200 为多批累计目标。
- **复合处理**：E3 测的是"激进截断"总效应（信息+缓存），E2 才是接近纯的损失→信念中介环。

## 8. 产出物清单

runs.jsonl（verdict）· sessions.jsonl（ifc+config 段）· hbe.jsonl（新世代完整答案）· manifest/*.jsonl · experiments.jsonl（窗口）· 分析报告 + PRD 状态更新。
