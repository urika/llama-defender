# BVR 自调优闭环设计（autotune loop）

> **状态**：已接受（Phase 1 实现依据）；**2026-10-05 裁决收窄（hub 0925-2①，域负责人拍板"分工"）**：本设计**限提案提取器范围**（generate/list）继续有效；**apply/rollback 生效路径冻结**——采纳与生效归 swe-eval ADR-011 双速循环（goal.yaml 白名单＋E 期周闸门），本模块不直接落参数。
> **日期**：2026-09-16
> **上游讨论**：《维特根斯坦与 LLM》21 轮对话结论——RSI 在强意义不可达（点火未达、
> 主语不可让渡、私人语言缺外部裁判），可落地的是 **BVR（Boundary-Verification
> Recursion）**：以失败面为唯一实体资产，地图是交付物，增长是废气。
> **本仓验证**：DEFECT-LIST 30 缺陷 + 配置实测锚点 + 影子代理留出判据 + R1-R19
> 契约追踪——治理层已是 BVR 现实样本（见 AGENTS.md §12 与仓库提交史）。

---

## 1. 设计目标与边界

**目标**：让系统的失败产物真实地改写系统的行为参数，形成"失败 → 提案 → 兑现 →
落参数 → 新失败"的闭环——但把决定权降维成提案权 + 兑现权，把签名权留给人和外部
固定判据。

**合法改造面**（只允许在这张表内落参数）：
- `proxy_config.py` `CONFIG_REGISTRY` 已注册的 **reloadable** 参数（压缩阈值、
  路由阈值、循环阈值、OOM 墙、队列桶阈值等）；
- 工具定义（`PROXY_TOOL_FILTER_*` / keep 集）；
- 路由策略（`models.json` route preferences / `PROXY_ROUTE_*`）。

**禁区**（提案永不触达）：模型权重、治理规则本身、主体边界项、`secret.local.conf`。

**诚实标注**：自动化的上限 = 判据的形式化程度。判据完全公共/可形式化（编译、测试、
证明、数值目标）时签名可自动化；开放判据（方向、品味、什么算对）签名永久留人。

## 2. 闭环架构（六层）

```
失败事实(FACT) ──► 提案提取器(AutoTune) ──► 兑现闸门(影子 A/B) ──► 分级签名 ──► 落参数(reload/conf)
    ▲                                                                                    │
    └────────────────── 审计落盘 logs/diag/autotune/ ◄───────────────────────────────────┘
```

### T1 事实通道（复用已有，补寻址规范）

已有事实流：`session_ledger`（动作轨迹）、`memory_stores` manifest（折叠丢弃索引）、
IFC 信号（retention / action_div / reread_pressure）、`hbe.jsonl`（信念熵）、
`diagnostics/sessions.jsonl`（per-turn 深度记录）、sent_view 档案、DEFECT-LIST。

新增统一寻址 `fact://<type>/<sid>/<seq>` + **三性标签**（依他=observed /
遍计=unverified_proposal / 圆成=verified）——把"概念生命周期"机读化，为遍计态
概念提供淘汰通道。

### T2 提案提取器（Phase 1 核心，规则引擎）

输入：失败事件流（loop 触发、失忆截断、prefix 击穿、OOM 驱逐、dup 重读、
advisor OFFTRACK）。输出结构化提案：

```json
{
  "proposal_id": "at_<ts>_<seq>",
  "target_param": "PROXY_LOOP_THRESHOLD",
  "from": "7", "to": "8",
  "evidence_ref": ["fact://loop/s38b..../014"],
  "prediction": {"metric": "loop_l1_rate", "delta": "-20%"},
  "risk_class": "low|medium|high",
  "rollback_cmd": "apply --id ... --revert"
}
```

**硬约束**：不生成代码补丁；只能改写合法改造面。注册表外新参数 = 强制人工 +
外部仪器通道（种子性决定）。每条提案必须带 `evidence_ref` 且附预言。

### T3 兑现闸门（影子环境）

复用 promptfoo 影子代理（:4021，状态隔离）+ 留出判据。同一组公共任务集跑
基线 vs 提案 A/B，N 轮。兑现判据 = 目标指标改进 ≥ 阈值 且无回归；预言-兑现
对账（预言 delta vs 实际 delta 偏差过大 → 提案无效，记入校准缺口）。

### T4 限速 + 失败面绑定

- 无 `evidence_ref` 的提案直接驳回（没有失败的地图不配改参数）；
- 扩能力类提案降权（只认"修失败"，不认"长能力"）；
- 每会话 / 每天晋级条数上限（默认 1 / 5，reloadable）。

### T5 分级签名

| 风险级 | 改造面 | 签名方式 |
|--------|--------|----------|
| low    | 压缩/路由阈值等 reloadable 参数 | 影子通过即自动应用 + 配置 diff 可回滚 |
| medium | 工具定义、路由策略、算子库 | 人一键审批 |
| high   | 治理规则本身、主体边界、新判据 | 永远人签 |

### T6 防判据博弈三件套

1. shadow 任务集由人固定，系统不可写（隐藏留出集）；
2. 三性标签淘汰：遍计态提案无兑现记录 → 自动回收；
3. 兑现测的定义 = "决策是否改变了结果"，不是"指标是否好看"（Goodhart 防护）。

## 3. Phase 1 实现清单（本仓增量）

| # | 工件 | 说明 |
|---|------|------|
| 1 | `autotune.py` | schema + fact 寻址 + 三性标签 + 审计落盘 + 提取器规则引擎 + 分级闸门（纯函数，stdlib only） |
| 2 | `manage.sh autotune` | `list / apply / rollback` 子命令（读提案、按风险级应用、回滚） |
| 3 | 审计落盘 | `logs/diag/autotune/<date>.jsonl`（提案/证据/预言/兑现/签名人），MB 上限轮转 |
| 4 | 单元测试 | `test/unit/test_autotune.py`（schema 校验、提取器规则、分级闸门、审计读写、限速） |
| 5 | 配置注册 | `PROXY_AUTOTUNE_*` 参数（enabled / daily_limit / default_signature）入 CONFIG_REGISTRY |

**非目标（后续 Phase）**：T3 影子 A/B 真实跑批（接 promptfoo 协议）、
事实通道与 `session_ledger` / IFC 的深度接线、`/api/autotune` 结构化端点、
提取器接入离线事件流（`sessions.jsonl` 批扫）。

## 4. 验收标准（Phase 1）

- `python3 -c "import autotune"` 零依赖导入；
- 从一段合成失败事件（loop/失忆/击穿/OOM/dup）能提取出合法提案，且禁区参数
  永不出现；
- 无 `evidence_ref` / 超限 / 禁区参数三类提案被闸门拒绝并有原因码；
- `./manage.sh autotune list` 能列出已落盘提案与风险级；
- `bash test/run_tests.sh --unit` 通过。

## 5. 与既有组件的关系

- 复用：`proxy_config.CONFIG_REGISTRY`（改造面白名单）、`reload_config`（落参数
  通道）、影子代理（T3 雏形）、`post_governance.PatternCompiler`（≥3 下沉，T2
  的认知同构）、`diagnostics`（审计载体）。
- 不动：`anthropic_proxy.py` / `pipeline.py` 运行路径（纯旁路，fail-open）。