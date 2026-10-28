# Flint 系列模型评估记录（2026-10-29 质量门 v3）

> **结论**：Flint 三个变体（flint-40b / flint-iq3 / flint-4bit）**均通过**质量门 v3，
> 但**均未超过现役主引擎 optiq-35B（93.5%）**；最好成绩 flint-4bit **88.7%**，低 4.8pp。
> **判定：暂不替换主引擎。** 若后续仍要评估，须补 agentic 批（resolve/墙钟），
> 本门只覆盖知识与格式面。
>
> 归属：测试由对端会话执行（引擎直连，不经代理）；本文档为本仓侧归档与判读口径。

## 一、四次跑数与结论

| # | 时间 | 模型 | base | 判词 | overall | 类别失守 |
|---|------|------|------|------|---------|---------|
| 1 | 01:27:54 | flint-40b | `:8086` | ❌ **FAIL** | 83.9%（52/62） | **summary 42.9% < 66.7%** |
| 2 | 01:33:11 | flint-40b | `:8086` | ✅ PASS | 85.5%（53/62） | — |
| 3 | 01:49:38 | flint-iq3 | `:8000` | ✅ PASS | 82.3%（51/62） | — |
| 4 | **03:03:41** | **flint-4bit** | `:8000` | ✅ **PASS** | **88.7%（55/62）** | — |

> **#1→#2 的翻转必须解释**：summary 42.9% → 71.4%，同模型同端口、间隔 5 分钟。
> 差异来源＝**thinking 预算**：thinking-on 时 768 max_tokens 被 `reasoning_content`
> 吃掉，正文空/截断（本仓 10-27 首次遇到：optiq extraction 11/13→13/13）。
> 质量门 v3 口径要求**显式 `enable_thinking=false`**；跨模型比较必须统一此口径，
> 否则 5pp 级的差别全是预算结构造成的假象。

## 二、最新一次逐类（flint-4bit，03:03，62 条）

| 类别 | 得分 | 线 | 判定 | 与 optiq-35B 对比 |
|------|------|-----|------|------------------|
| extraction | **100%** (13/13) | 75.0% | ✓ | 持平（12/13，flint 更稳） |
| tagging | 90.0% (9/10) | 87.5% | ✓ | **低**（optiq 10/10） |
| summary | 71.4% (5/7) | 66.7% | ✓ | **持平**（optiq 5/7） |
| grounding | **100%** (9/9) | 66.7% | ✓ | **高**（optiq 9/9 持平） |
| refine | 83.3% (5/6) | 50.0% | ✓ | **低**（optiq 6/6） |
| coding | 70.0% (7/10) | 62.5% | ✓ | **低**（optiq 9/10） |
| tool_call | **100%** (7/7) | 80.0% | ✓ | 持平（optiq 7/7） |

**失手 7 条**：`C01_news_summary`、`C07_en_summary`（摘要）、`COD03_camel_to_snake`、
`COD04_extract_versions`、`COD06_shell_find`（编码）、`B09_multi_label`（多标签）、
`E06_en_translate`（翻译）。

其中 **COD04（版本正则词边界）是跨代老难点**——optiq-35B / ornith-9B / minicpm-2B 三代
模型一致栽在此题，属金标已知难点，不构成 Flint 的独立劣势。

## 三、横向对照（同一门，同一口径）

| 模型 | 判词 | overall | 备注 |
|------|------|---------|------|
| **optiq-35B（现役主引擎）** | **PASS** | **93.5%（58/62）** | 唯一**全类别过线**；summary 5/7、tagging 10/10 |
| flint-4bit | PASS | 88.7%（55/62） | 2026-10-29 03:03（本文最新） |
| ornith-9B（已下线） | FAIL | 87.1%（54/62） | summary 2/7 失守 |
| flint-40b | PASS | 85.5%（53/62） | 01:33（thinking-off 后） |
| flint-iq3 | PASS | 82.3%（51/62） | 01:49 |
| minicpm-2B（已删除） | FAIL | 71.0%（44/62） | tool_call 0/7 |

## 四、判读边界（不可外推的部分）

1. **本门只测知识与格式**（62 条 / 7 类，`test/fixtures/knowledge_gate_9b.json`），
   **不含 agentic 能力**（resolve 率、墙钟、多轮工具编排）。P3 终裁已明确：模型层证据
   ＝吞吐面＋本门，agentic 结论必须来自批跑（`exp-20261024-p0-northstar-repro` 类）。
2. **直连引擎、不经代理**：未经 F1/F2/F3'/压缩/路由等代理层加工；生产表现可能不同。
3. **temperature=0、单次跑**：未做多种子；5pp 级差异应视为单点噪声量级，
   不宜据此做「谁更聪明」的结论（差异主要体现在 tagging/refine/coding 三类的 1–2 题）。
4. **未做能力档（27B-mlx）对照**：27B 在 P3 终裁中被降为备件，本轮未纳入。

## 五、若要真的换模——准入清单（缺一不可）

| # | 项 | 依凭 |
|---|----|------|
| 1 | agentic 批对照（同实例、同 target、分段声明） | resolve 率 + 墙钟；J6 行为类 |
| 2 | 质量门 v3（thinking-off，62 条） | 本文 |
| 3 | `configs/<model>.conf` + `capability_audit.py` exit 0 | ADR-017 §2（换模必跑） |
| 4 | 四旗标随迁（F1/F2/F3'/F3 + br4 录制面） | `docs/05-operations-changelog/fnf-flags-activation-20261027.md` / `f7814f6`（27B conf 同步先例） |
| 5 | **内存账本核算**：与 35B 引擎（22GB）、kapitsa（13GB）的互斥关系 | `docs/06-reference-metrics/memory-ledger-arbitration-20261029.md` |
| 6 | 空窗一次戳（conf sha256 + 窗前后 /metrics 快照） | 激活戳记先例 |

## 六、运行面事实（与内存账本的交集）

测试期间 `:8000`（kapitsa / flint-iq3 / flint-4bit）与 `:8086`（flint-40b）与 35B 主引擎
**同时驻留** —— 35B 22GB + flint 13GB 级 → 宿主机内存 92%，触发代理 fail-fast：

```
{"error": {"type": "backend_oom", "message": "System memory pressure is high (used 92.0%)."}}
```

→ 即「服务活着但全拒」。测试结束后 `:8000`/`:8086` 均已停止（现仅 `:8081` 主引擎 + `:8083`
embedgemma2），内存恢复正常。**这正是互斥纪律要防的场景**，见
`docs/06-reference-metrics/memory-ledger-arbitration-20261029.md` §2/§3。

## 七、产物索引

| 产物 | 路径 |
|------|------|
| flint-40b 首跑（FAIL，thinking 未关） | `logs/knowledge_gate/gate-20261029-012754.json` |
| flint-40b 复跑（PASS） | `logs/knowledge_gate/gate-20261029-013311.json` |
| flint-iq3（PASS） | `logs/knowledge_gate/gate-20261029-014938.json` |
| **flint-4bit（PASS，最新）** | `logs/knowledge_gate/gate-20261029-030341.json` |
| 质检工具 | `tools/knowledge_gate_9b.py`（62 条金标 / 7 类，`--thinking` 开关控制思考预算） |
| 现役基线 | `docs/06-reference-metrics/BENCHMARK.md` §质量门 v3 跨模型基线 |
