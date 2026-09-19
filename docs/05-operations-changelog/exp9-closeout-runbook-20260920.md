# EXP-9 收官日操作手册（Runbook）

> 用途：EXP-9 收官日的三并行动作（D5 激活 / 收官取证·判读[swe-eval 主导] / 召回 A/B 微窗口[swe-eval 主导]）+ EXP-8 窗口的 LCP ladder 序列——**照抄执行，零现场决策**。
> 依据：联合协调文档 J3 排程 / J5 激活约定 / J2 LCP 方案；llama.cpp 侧认领部分为主。
> 前置：本手册为 2026-09-20 预写；执行前先 `git log` 确认无新协调变更（联合文档为唯一权威）。

## 0. 收官判定（谁先谁后）

```bash
# EXP-9 驱动退出 = 收官信号（swe-eval 侧确认，llama.cpp 只复核）
ps -p 4579 > /dev/null && echo "仍在途" || echo "驱动已退出"
curl -s localhost:4000/api/status | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['state'])"
```
收官后**先跑完 swe-eval 的收官取证（M-1②）再动代理**——取证含代理侧数据拉取，代理重启会清内存台账（R14 ledger/R16 聚合），**顺序硬约束：取证 → 重启**。

## 1. D5 激活：ctx_config 两开关点亮（llama.cpp 主导）

**注意：代码激活需要重启代理进程（Python 代码变更，SIGHUP reload 不加载新代码）；只重启代理，不动引擎（引擎重启=破坏 A0 世代，属 EXP-8 窗口）。**

```bash
# ① 确认无在途请求（队列空 + 无活跃 swe 会话）
curl -s localhost:4000/api/queue | python3 -m json.tool | grep -E "waiting|workers"

# ② 仅重启代理（引擎已运行会被 start 跳过，不重载模型）
kill $(cat anthropic_proxy.pid) && sleep 2 && ./manage.sh start

# ③ 验证清单（三项全过才算激活成功）
curl -s localhost:4000/api/status | python3 -c "
import json,sys; d=json.load(sys.stdin)
ctx = d['ctx_config']
assert d['api_version'] == '2', 'api_version 异常'
assert 'auto_recall_enabled' in ctx and 'pd_micro_turn_enabled' in ctx, '两开关未出现'
assert ctx['pd_micro_turn_enabled'] is True, 'IFC-3 现值应 true'
assert ctx['auto_recall_enabled'] is False, 'auto-recall 应仍 false(A/B 前不开)'
print('D5 激活验证通过:', {k: ctx[k] for k in ('auto_recall_enabled','pd_micro_turn_enabled')})"

# ④ 回写联合文档（待办#1 状态：激活完成）
```

## 2. 召回 A/B 微窗口（swe-eval 主导，llama.cpp 配合点）

- micro exp yaml（待办#3，swe-eval 起草）触发臂切换暂停点 → **llama.cpp 动作仅一处**：
  `PROXY_AUTO_RECALL_ENABLED=false→true` 改 conf + `./manage.sh reload`（行为类开关 reloadable，无需重启代理）→ touch ack；
- 臂元数据：每臂开跑前抓 `GET /api/status` ctx_config 快照（契约 §4.5，两开关现值自包含）；
- 判读指标三件套：dup 触发率 / 注入后采用率 / 幻觉计数（定义见 `swe-eval/docs/recall-ab-design.md`；机读 kind=`auto_recall`/`value_check`）；
- **一票否决**：recall 臂 value_check↑ 或编造标识符↑。

## 3. EXP-8 窗口：换模重启 + LCP off 臂对照（双方协调）

```bash
# ① 换模到 Qwen3.8-27B conf（EXP-8 主批用，27b conf 现成）
./manage.sh switch qwen3.8-27b-4bit && ./manage.sh stop-backend

# ② 引擎重启 @flag OFF —— off 臂测量（bench ladder，分钟级）
#    conf 临时注释 PROXY_CACHE_LCP_SNAPDOWN=1 → ./manage.sh start-backend
python3 tools/bench_ctx_ladder.py            # off 基线（记录 cold TTFT/prefill）
python3 tools/lcp_trace_analyzer.py --archive  # off 段遥测归档（应零 snapdown 行）

# ③ 引擎重启 @flag ON —— on 臂测量
#    conf 恢复 PROXY_CACHE_LCP_SNAPDOWN=1 → ./manage.sh stop-backend && ./manage.sh start-backend
python3 tools/bench_ctx_ladder.py            # on 对照
python3 tools/lcp_trace_analyzer.py --archive  # on 段归档

# ④ Gate B 判读材料齐：两段 ladder + 两段 lcp_trace 归档 + EXP-9 期间的 on 侧生产画像
#    （已有基线: logs/diag/lcp_trace/stats-20260920T002413.json）
```

- LCP 为缓存层加速，**透明不进 EXP-8 判定口径**（J2）；
- 引擎重启协调协议照走（多会话通知 + 重启窗口 404 预期，`engine-restart-coordination-20260906.md`）。

## 4. 值守例行动作（窗口全程）

> **2026-09-20 起已自动化**：`tools/exp9_watch.py` 每 2 小时定时执行（驱动存活/turn
> 新鲜度/信号量审计/水位/**sessions.jsonl 轮转防护归档**——轮转只留 1 备份，EXP-9
> 剩余时段预计再转 2+ 次，归档目录 `logs/diag/evidence-exp9/`）；驱动消失时自动
> 终扫归档并报收官信号。手动复查命令如下。

```bash
ipcs -s | wc -l                             # L-30 信号量审计（P0 风险）
python3 tools/lcp_trace_analyzer.py         # 增量遥测速览
curl -s localhost:4000/api/sessions | head  # 会话健康
```
