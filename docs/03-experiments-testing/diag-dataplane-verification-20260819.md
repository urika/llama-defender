# R13-R16 诊断数据面验证方案与实测记录

> 状态：v1.0（2026-08-19，随 R13-R16 交付同步建立；L0-L2 已全绿，L3 待 agent_go 接入）
> 被测对象：[diagnostics-dataplane-design-20260819.md](../02-architecture-design/diagnostics-dataplane-design-20260819.md)（commit `37937ce`）
> 验收基线：[llama-defender-integration-requirements.md §3.2/§5](../llama-defender-integration-requirements.md)
> 环境：rapid-mlx + Qwen3.8-27B-4bit（active.conf），`PROXY_DIAG_ENABLED=true` 默认开

---

## 0. 验证总览（五层金字塔）

| 层 | 验证什么 | 方法 | 状态（2026-08-19） |
|----|---------|------|-------------------|
| **L0 自动化回归** | 纯函数数学/契约不回归 | unit 1098（含新增 45）/ signature / snapshot / integration 10 组 / promptfoo | ✅ 全绿 |
| **L1 冒烟** | 重启后服务通、字段出 | 30 秒命令组（§2） | ✅ 通过 |
| **L2 场景化验收** | 每个 R 的核心语义 | 构造性请求 + 端点核对（§3-§6） | ✅ A/B/C 三场景 + 冒烟矩阵 |
| **L3 消费方验收** | agent_go 端到端消费 | agent_go 侧三件套接入（§8） | ⬜ **唯一未闭环层** |
| **L4 运行期验证** | 有界性/开关回归/漂移监控 | 上线后持续观察（§7） | 🟡 观察中 |

---

## 1. 每 R 验收矩阵（对应需求文档 §5 验收列）

| 需求 | 验收标准（§5 原文） | 代理侧验证 | 消费方验证 |
|------|-------------------|-----------|-----------|
| R13 诊断响应头 | metering 采集 Processed-N / Epoch-Count / Feedback-Injected | L1 头/尾注出数 + L2-A 注入触发 | agent_go `api.py:156` 双来源解析 → metering.jsonl 字段 |
| R14 台账端点 | 轮级看门狗消费 dup / last_dup_turn / 材料清单 | L2-B dup 计数 | agent_go `subtask.py` 轮询 ledger，dup≥3 → rabbit_hole 事件 |
| R15 档案查询 | 压缩后行为复盘以代理档案为准（L4 只读） | L2 archive 索引/正文回放 | agent_go `eval.py` 形态学复盘改读 `?view=sent` |
| R16 /metrics 会话维度 | 每轮 jsonl 落盘 + session 聚合，A/B 出数 | L2-C 双账本关联 + 聚合端点 | bench manifest 读 `/api/status` `ctx_config` 口径标注 |

---

## 2. L1 冒烟命令组（每次重启后 ~30s）

```bash
# ① 状态字段（backend.name / ctx_config）
curl -s localhost:4000/api/status | python3 -m json.tool | grep -A5 -e backend -e ctx_config
#    预期：state=healthy, backend.name=rapid-mlx（需 manage.sh export 修复后重启）,
#          ctx_config.diag_enabled=true

# ② 流式尾注（R13 流式通道）
curl -sN -X POST localhost:4000/v1/messages -H "Content-Type: application/json" \
  -H "X-Claude-Code-Session-Id: smoke01" \
  -d '{"model":"claude-3-5-sonnet","max_tokens":16,"stream":true,"messages":[{"role":"user","content":"hi"}]}' \
  | grep "^: x-proxy-diag"
#    预期：: x-proxy-diag {"request_id":"req_..."}（有 timings 的后端另含
#          prompt_processed_n/prompt_sent_n/hit_ratio）

# ③ 非流式头（R13 头通道）
curl -s -D - -o /dev/null -X POST localhost:4000/v1/messages \
  -H "Content-Type: application/json" -H "X-Claude-Code-Session-Id: smoke01" \
  -d '{"model":"claude-3-5-sonnet","max_tokens":16,"stream":false,"messages":[{"role":"user","content":"hi"}]}' \
  | grep -i x-proxy-diag
#    预期：X-Proxy-Diag-Request-Id 必现；Prompt-Processed-N 仅后端有 timings 时

# ④ per-turn 落盘（R16）
tail -1 logs/diag/sessions.jsonl | python3 -m json.tool
#    预期：schema_version=1, request_id, session_key, turn, backend.name=rapid-mlx

# ⑤ 会话发现 + 台账（R14；key 取列表返回的 8 字符值）
curl -s localhost:4000/api/sessions
curl -s "localhost:4000/api/session/<key>/ledger" | python3 -m json.tool | head -30
```

---

## 3. L2-A 注入可观测性（R13 核心：Feedback-Injected）

**构造**：连续 `PROXY_BLOCKER_THRESHOLD`(2) 次同型错误 tool_result 且**以 tool_result 结尾**（尾部纯文本会中断「连续错误尾」判定——这是检测器语义，不是 bug）：

```python
msgs = [{"role":"user","content":"test"}]
for i in range(2):
    msgs.append({"role":"assistant","content":[{"type":"tool_use","id":f"t{i}","name":"Read","input":{"file_path":"/nope.py"}}]})
    msgs.append({"role":"user","content":[{"type":"tool_result","tool_use_id":f"t{i}","content":"Error: file does not exist"}]})
# 不加尾部纯文本消息
```

**实测结果（2026-08-19）**：
- 响应头：`X-Proxy-Feedback-Injected: blocker` ✅
- jsonl：`feedback_injected: ['blocker']` ✅（三通道一致：头 / SSE 尾注 / 落盘）

**kind 全集**（7 处注入点）：`blocker` `route_notice` `session_loop_warning` `loop_l1/l2/l3` `text_loop` `reread_hard` `high_drop_notice` `truncation_summary`；触发条件见各 stage（pipeline.py stage 2.6/4/10/11/12/14/15）。

## 4. L2-B dup 计数（R14 核心：轮级看门狗数据源）

**构造**：单请求历史含 3 次**同查询** WebSearch（规范化 hash 相同——大小写/空白/URL 查询串差异归并）。

**实测结果**：
```json
dup_queries: [{"tool":"WebSearch","target":"github ansible pull 80376","count":3,"first_turn":1,"last_turn":1}]
action[2]:   {"turn":1,"tool":"WebSearch","target":"github ansible pull 80376","dup":3,...}
```
✅ 与上游设计 §4.2 台账行（`dup=4 | last=#48`）语义一致；看门狗判定 `dup≥3 → rabbit_hole` 可机读。

**边界**（上游设计 §11.5）：hash 规范化只抓「同查询重复」；换查询兔子洞与训练先验幻觉不在覆盖内——dup 是必要非充分指标，勿以其替代无效轮占比的完整形态分类。

## 5. L2-C 双账本关联（R16：request_id 互查键）

**方法**：取 `logs/diag/sessions.jsonl` 最近 N 条 `request_id`，join `logs/proxy_metrics.jsonl`。

**实测结果**：5/5 关联（session_id/status/duration_ms 对齐），含真实流量会话（`cli_ac93`）✅。metering ↔ 深度记录互查通道成立。

## 6. L2 剩余项（按需执行）

| 项 | 方法 | 通过标准 |
|----|------|---------|
| archive 正文回放（R15） | `curl "…/archive?turn=N&include_payload=true"`，对 `payload` 字段 `json.loads` | 还原该轮实际发给后端的 messages；`payload_truncated` 仅超 400KB 时 true |
| OpenAI 协议路径 | 同样请求改发 `/v1/chat/completions` | 尾注在 `data: [DONE]` 之前；ledger/archive 同样出数 |
| 云端 anthropic 透传路径 | 切 zhipu（protocol: anthropic）配置后流式请求 | 尾注**在 `message_stop` 之前**插入（评审修复 77e9b10：透传循环内拦截 `message_stop` 字节写入——流尾追加会被解析器丢弃） |
| 诊断层异常可见性 | 人为制造诊断异常（如临时改坏 jsonl 路径权限） | WARN 日志按挂点名且限频（`warn_suppressed`），请求仍成功（fail-open 不静默，评审修复 77e9b10） |
| 降级矩阵 | `/api/backend/props`（rapid-mlx 必 501）、`?view=canonical`（必 501）、不存在 key（必 404） | 结构化 JSON 错误体，消费方 fail-open |
| 注入 kind 扩展验证 | 构造 5+ 次同工具调用（loop_l1）、超限截断（truncation_summary）等 | 对应 kind 出现在头/尾注/jsonl |

---

## 7. L4 运行期验证（持续观察）

| 项 | 观察方法 | 告警口径 |
|----|---------|---------|
| 磁盘有界 | `du -sh logs/diag/` | 超 `PROXY_DIAG_ARCHIVE_MAX_MB`(200) 自动删最老会话文件 |
| 内存有界 | `/api/sessions` 会话数 | ≤ `PROXY_DIAG_SESSION_MAX`(64)；TTL 180min 后端点返回 410 |
| 开关回归 | `PROXY_DIAG_ENABLED=false` + `./manage.sh reload` 后发请求 | 零诊断头/零尾注/零落盘（设计 §8：关=零开销零字段） |
| `canonical_mismatch` | `logs/lifecycle_events.jsonl` 频率 | 频繁 >0 = 客户端自行裁剪历史 → 缓存收益打折前兆（上游设计 §4.7.4） |
| 会话 key 污染 | `/api/sessions` 的 `key_source` | 出现大量 `fallback` = 有客户端没发 `X-Claude-Code-Session-Id`（按天合并，台账污染） |

## 8. L3 消费方验收清单（agent_go 侧）

> 2026-08-19 更新：代理侧接口审计发现的 G-A~E 缺口已全部补齐并活体验证（api_version="2"、`proxy_diag` 体字段、尾注含 `session_key`、端点接受完整 key、ctx_config 有效压缩状态）——agent_go 侧接入零妥协。

1. **metering 双来源解析**：`api.py:156` 的 R8 头解析扩展为「HTTP 头（非流式）+ SSE 注释行 `: x-proxy-diag {...}`（流式）」——两个通道字段同名同义；metering.jsonl 增字段 `prompt_processed_n / hit_ratio / epoch_count / feedback_injected[] / diag_request_id / session_key`。
2. **批跑 harness 显式发送 `X-Claude-Code-Session-Id`**（key 契约，见需求文档 R14 节；端点已接受完整值，无需自行截断）。
3. **bench manifest 口径标注**：读 `GET /api/status` 的 `ctx_config` 段（含 `compress_enabled / compression_profile / bm25_enabled` 有效状态 + S/K）。
4. 形态学复盘切换数据源：`GET /api/session/<key>/archive?view=sent`（弃用 claude CLI 客户端转录——视角错位）。

---

## 9. 已知边界（验证时视为预期，非缺陷）

1. **rapid-mlx 无 timings（2026-08-19 定案）**：非流式响应无 `timings` 键，流式终块连 `usage` 都没有 → `X-Proxy-Prompt-Processed-N` / `hit_ratio` / 流式 `prompt_sent_tokens` **保持缺省/null，不发假值**（设计 P1/P3）。缓存命中率数据走离线 `tools/cache_analyzer.py`（解析后端日志 `cache_fetch`/`schedule` 行）；切 llama-server 后字段自动点亮（进程级探测 `backend.timings_supported`）。
2. **`Epoch-Count` / `is_epoch_turn` / archive `canonical` 视图**：预留 null / 501，上下文工程 Phase 1 落地后点亮——字段名已定死，消费方代码可先行编写。
3. **会话 key 8 字符截断**：`/api/session/<key>/...` 的 key 必须用截断后的值（`/api/sessions` 列表返回的就是）；`manage.sh route-force-*` 的同名不对称问题已文档化（需求文档 R14 节）。
4. **非流式首个请求 duration 偏大**：重启后模型预热（首请求 ~900ms+），验证延迟类字段应看稳态。

## 10. 实测环境快照（复现基准）

- commit 链：`37937ce`（R13-R16 落地）→ `77e9b10`（评审修复：透传路径尾注改在 message_stop 前插入、诊断异常 WARN 可见性 `warn_suppressed`、timings 探测重置）→ `8e56bd0`（manage.sh `export LLAMA_BACKEND` + 上游设计 §11 实测校准入库）
- 后端：rapid-mlx（127.0.0.1:8081），Qwen3.8-27B-4bit，prefix cache on / KV q4 / pflash 96K
- 代理：127.0.0.1:4000，`PROXY_DIAG_*` 全默认（enabled/sse_tail=true, ttl=180min, max=64, archive=200MB）
- 冒烟/场景请求均为 max_tokens≤32 的小请求（本地零成本）
