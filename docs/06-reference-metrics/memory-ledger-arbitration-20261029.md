# 内存账本仲裁（48 GB 单机多模型共存纪律，2026-10-29）

> 面向运维与值班：**谁能同时驻留、谁必须互斥、撞了什么症状、怎么恢复**。
> 本文档由来：2026-10-27～10-29 两天内 **4 次内存账本耗尽**（3 次整机重启 + 1 次外来
> 模型服务器挤占），全部根因同一句话——**48 GB 装不下「本地 35B 引擎 + 第二个模型
> 服务器 + 多 IDE/agent 会话」**。

## 1. 内存账本（48 GB Mac，实测口径）

| 常驻项 | 实测占用 | 说明 |
|--------|---------|------|
| Splash 35B-A3B 引擎（`serve-native` Metal） | **约 22 GB**（`splash_memory_current_bytes` ≈ 23.8 GB，headroom ≈ 19 GB） | 主力档；wired 内存（驱动持有） |
| Splash 27B 稠密引擎 | 约 15–19 GB | 能力档，比 35B 省约 5–7 GB |
| embedgemma2（:8083，CPU 引擎） | 约 0.3–0.5 GB | 可常驻，代价小 |
| **kapitsa（:8000，`serve --model flint-iq3`）** | **13.1 GB** | 外来第二模型服务器（2026-10-28 晚由其它会话引入） |
| IDE / agent 会话（ZCode helper + opencode + zcode-cli + claude + iTerm） | 合计约 3–4 GB | 多会话并行时逐个累加 |
| macOS 自身（含 compressor） | 约 4–6 GB | compressor 常态 5–10 GB 压缩页 |

**结论：35B 引擎（22 GB）+ kapitsa（13.1 GB）＝ 35 GB 起步，再加 IDE 与系统 → 必超 48 GB**，
只能靠 swap 硬撑；swap 一旦到 90%+ 就进入互相拖慢的换页地狱。

## 2. 互斥纪律（硬规则）

1. **35B/27B 本地引擎 与 kapitsa 二选一**——不得同时驻留。
   要起第二个模型服务器，先 `./manage.sh stop-backend`（释放 22 GB），用完再 `start-backend`。
2. **换模/起第二引擎前先查账本**：
   ```bash
   sysctl -n vm.swapusage          # used 应 < 2 GB
   vm_stat | awk '/Pages free/{printf "free %.1fGB\n", $NF*16384/1073741824}'   # 应 > 20 GB
   ```
3. **批跑期禁起第二模型服务器**（同「批期禁切换」红线）：会直接把在飞批跑的内存挤爆，
   表现为 denied 尖峰 + TTFT 暴涨 + 代理 503。
4. **多 IDE 会话不是免费的**：3–4 GB 虽小，但在 90% 临界线上足以把服务推过闸门。

## 3. 撞上之后的症状（三层防线，按触发顺序）

| 层 | 触发条件 | 表现 | 配置 |
|----|---------|------|------|
| ① 代理 fail-fast 闸 | 宿主机 used% > `PROXY_MEMORY_REJECT_THRESHOLD`（**90%**） | HTTP `{"error":{"type":"backend_oom","message":"System memory pressure is high (used 92.0%)","retryable":true}}`——**服务「活着但全拒」**，最容易被误判为引擎故障 | `PROXY_MEMORY_ENABLED` / `PROXY_MEMORY_REJECT_THRESHOLD` |
| ② 路由降级 | used% > `PROXY_ROUTE_MEMORY_PCT`（默认 90%） | 请求**规则性改走云端**（日志 reason=`memory_pressure(92%/4GB)`）——本地引擎空转、成本上升 | `PROXY_ROUTE_MEMORY_PCT` |
| ③ 监控告警 | swap > `MONITOR_SWAP_MB`（6144，>90% 即 CRIT）；denied 速率 > 300/h | 日志 `⚠ swap 近满 …需重启系统` / `⚠ denied 增速 …` | `MONITOR_*`（launchd 注入） |

**判读要点**：看到 `backend_oom` 不要以为引擎坏了——先看 `vm_stat` 空闲页与 swap；
引擎侧 `splash_memory_pressure=normal` 而代理拒绝，就是**宿主账本**问题，不是引擎问题。

## 4. 恢复流程（按代价从低到高）

```bash
# 1) 先看谁在吃内存（13 GB 级的外来进程一眼可见）
ps -Ao rss=,pid=,command= | sort -rn | head -5 | awk '{printf "%.2fGB  %s\n",$1/1048576,$0}'
lsof -Pi :8000 -sTCP:LISTEN -t      # 常见外来模型服务器端口

# 2) 停掉外来模型服务器（优雅优先；它不是本栈进程，停前记录 pid/命令/连接数）
kill -TERM <pid>                    # 等 20s，不退再 kill -9

# 3) 复核空闲页是否回来（macOS 惰性回收，通常数秒内 free pages 涨 10GB+）
vm_stat | awk '/Pages free/{printf "free %.1fGB\n", $NF*16384/1073741824}'

# 4) swap 不降是正常的（惰性回收）——但若 swap >90% 且仍报 backend_oom，
#    唯一彻底解是重启系统（见 docs/05-operations-changelog/system-reboot-20261027.md）
```

**恢复后确认**：`curl -s http://127.0.0.1:4000/api/status` → `state=healthy`；
对代理发一条带 nonce 的短请求（避免幂等去重 429）应正常返回；再跑
`bash tools/post_reboot_check.sh`（若非重启则不必要）。

## 5. 自动化现状（已就位，不需要人工盯）

| 机制 | 行为 | 位置 |
|------|------|------|
| 代理内存闸 | >90% 全拒（fail-fast，避免排队拖死） | `pipeline.py` / `PROXY_MEMORY_REJECT_THRESHOLD` |
| 监控 swap 判据 | >6 GB WARN、≥90% CRIT，文案直指「引擎重启救不了，需重启系统」 | `tools/monitor_window.py:swap_alert` |
| 监控清场 | 批跑连续缺席 ≥2 轮 + 累计 denied>500 ⇒ 自动 restart 清 KV/state | `tools/monitor_window.py:recycle_needed` |
| 守栈作业 | launchd `com.llama-defender.stack` 常驻守栈（wrapper 不退出，避免作业组连坐） | `tools/startup_stack.sh`（DEF-324/325） |
| 监控守护 | launchd `com.llama-defender.monitor`（KeepAlive，`MONITOR_AUTO_RESTART=1`） | `tools/launchd/` |

## 6. 历次事故索引（同一根因的 4 例）

| 日期 | 触发 | swap 峰值 | 处置 |
|------|------|----------|------|
| 2026-10-27 05:52 | 批跑重实例累积 | 10.44 GB / 11.26 GB | 重启系统 |
| 2026-10-28 22:55 | 批跑 + 影子探针累积 | 11.70 GB / 12.29 GB | 重启系统（并修 DEF-324 守栈连坐） |
| 2026-10-29 01:21 | 人工 `stop-backend` 让位 | 16.4 GB / 17.4 GB | 引擎停（半瘫）→ 修 DEF-325 守栈误判 |
| 2026-10-29 01:46 | **外来 kapitsa 13.1 GB 挤占** | 16.4 GB / 17.4 GB | 停 kapitsa → 服务秒恢复（本文档由来） |

> 共性教训：**每次都是「多一个 13–22 GB 的常驻者」**。每次只清 swap（重启）而不解决
> 共存纪律，第二天必然复发——4 天 4 次即证据。
