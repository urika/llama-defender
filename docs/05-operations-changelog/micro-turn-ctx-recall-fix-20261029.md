# ctx_recall 死环修复 + micro-turn 代答启用（2026-10-29 10:28）

> 触发：模型输出「Context overflowed. Let me recover the folded content from the session store.」
> 后连续 4 次 `Error: No such tool available: ctx_recall`，补救路径死掉并成环。
> 用户指令：做 1（重启代理载入修复）+ 2（启用 micro-turn 代答并探针验证）。

## 1. 根因（两处叠加）

| # | 问题 | 证据 |
|---|------|------|
| ① 代码层：**注入门与代答门不配对** | `_inject_ctx_recall` 只看 `PROXY_PD_ENABLED`（缺省 true）就广告 ctx_recall；而代答通道要 `PROXY_PD_MICRO_TURN_ENABLED`（缺省 false） | 会话 88845194（claude-cli 2.1.312）07:48–07:52 连回 4 次 `No such tool available`；代码内注释已有同型先例（38/38 零开火、死环 38 轮） |
| ② 运行层：**进程比 conf 旧 22 秒** | conf `08:40:01` 已写入 `PROXY_PD_MICRO_TURN_ENABLED=true` / `..._HOLD_TEXT=true`；代理 `08:39:39` 启动 → 一直跑旧旗标（实测 `ps -wwE` 为 false） | 时间戳对比 + 运行进程 env 实读 |

⇒ 净效果：工具被广告、模型调得着，但代理不应答、客户端工具表也没有它 → 客户端报错 → 模型的「恢复折叠内容」路径失效。

## 2. 处置

| 步骤 | 内容 |
|------|------|
| 代码修复 | `tool_filter._inject_ctx_recall` 增加 `PROXY_PD_MICRO_TURN_ENABLED` 判据：**不能应答就不广告**（提交 `522cfed`，unit 2120 全绿，新增「PD 开 + MICRO_TURN 关 → 不注入」用例） |
| 载入修复 | **只重启代理**（后端不动）：PID `93436 → 14644`；后端保持 `93356` |
| 旗标生效 | 重启后进程 env 实测：`PROXY_PD_ENABLED=true`、`PROXY_PD_MICRO_TURN_ENABLED=true`、`PROXY_PD_MICRO_TURN_HOLD_TEXT=true` |
| 戳记 | conf sha256 `e8d2f091…`；工件 `logs/ops/micro-turn-activation-20261029-102748/`（metrics/status 窗前后快照） |

## 3. 探针验证（代答链路端到端）

请求：新会话 `mt-probe-*`，tools 只给 `Read`，要求模型**必须**调用 `ctx_recall`。日志实证：

```
-> Tools: ['Read', 'ctx_recall']                                    ← 注入生效
-> [MICRO_TURN] non-stream ctx_recall self-answered; re-dispatching  ← 模型调了，代理自答并重派第 2 轮
<- backend status: 200（第 2 轮）
<- Responding: 工具可用。                                            ← 客户端只拿到纯文本，无死工具
```

- 响应块：`[('thinking', ''), ('text', '工具可用。')]` —— **无 ctx_recall tool_use 外泄**
- 引擎窗前后指标不变（failed 0；ttft_p50 2.36s；decode 119.1 tok/s）

## 4. 顺带发现（判读须知）

- 重启同时载入了并行会话在 conf 中的**未提交改动**：`PROXY_LOOP_BASH_PATH_KEYS_ENABLED` 由 `true → false`
  （F1/F2/F3' 仍 true）⇒ **fnf 治疗臂口径变为「F1+F2+F3'，旧 F3 兜底面关闭」**。该行我未提交（属对端在途改动），
  但**运行面已生效**——swe-eval 的 A/B 判读须按此分段记录。
- `PROXY_PD_MICRO_TURN_ENABLED` / `_HOLD_TEXT` **不在 `_RELOAD_SPEC`**（SIGHUP 不生效，只能重启）。
  本次正是「conf 改了但没重启」导致 22 秒的旗标错位——**改这两项必须重启代理**。

## 5. 回滚

```bash
# 关 micro-turn（回到「不广告 ctx_recall」，由 522cfed 的注入门保证不再死环）
sed -i '' 's/^export PROXY_PD_MICRO_TURN_ENABLED=.*/export PROXY_PD_MICRO_TURN_ENABLED=false/' configs/splash-qwen36-35b-optiq.conf
kill -TERM $(cat anthropic_proxy.pid) && ./manage.sh start    # 不重启后端
```

## 6. 结构性加固（同批落地，防同类复发）

两个问题都是**类别性**的，不修机制就会再来。已补三道：

| 加固 | 内容 | 验收 |
|------|------|------|
| ① 配对自检 | `anthropic_proxy.main()` 启动日志新增 `ctx_recall: injected/disabled`；PD 开而 MICRO_TURN 关时打 **WARN**，明示「模型将看不到 ctx_recall，需置 true 并重启（不在 `_RELOAD_SPEC`）」 | 启动日志可见 |
| ② 进程 vs conf 漂移检测 | 新增 `tools/flag_drift.py`（纯函数）＋ `tools/flag_drift_check.sh`（CLI，`--json/--quiet`，有漂移 exit 1）＋ **监控每轮自动跑**（`flag_drift_count` 入快照、出 WARN 告警） | 当前「✅ 无漂移（66 键）」；人为造漂移 → 正确报出并给修复路径；单测 14 例 |
| ③ conf 解析器修 bug | `_parse_conf_env` 无引号值的**行尾注释**此前被吞进值里（`PROXY_COMPRESS_ENABLED` 被解析成 `"false   # 上下文工程引擎接管"`）；现按 bash 规则剥离（`#` 前有空白才算注释） | 单测 4 例；漂移误报由 1 → 0 |

> **本加固的判据**：以后任何「conf 改了但服务行为没变」的怀疑，先跑
> `tools/flag_drift_check.sh`——它直接对 `ps -wwE` 与 active.conf，一眼看出是
> 「reload 就能修」还是「必须重启」，且不依赖 capability_audit 的 conf-only 视角。
