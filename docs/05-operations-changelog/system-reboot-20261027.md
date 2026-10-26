# 系统重启（2026-10-27 05:50）——swap 清零 + 栈自恢复

> 触发：swap 持续膨胀至 **10.44GB / 11.26GB**（动态 swap 已近满）、compressor 11.8GB、
> 引擎内存准入拒绝（denied）长期高位。swap 近满时引擎重启可能因
> 「not enough free memory to start」失败（DEF-320 同款），故择机重启整机。
> 用户指令：2026-10-27 重启系统。

## 1. 重启前状态（体检留档）

| 项 | 值 |
|----|-----|
| 在飞批跑/影子探针 | 无（`run_agent.py` / `shadow_probe.py` / `swe_test exp` 全无） |
| 代理 | pid 39619，state healthy（:4000） |
| 引擎 | Splash 1.4.0 + `Qwen3.6-35B-A3B-OptiQ-4bit`（:8081） |
| swap | total 11264M / **used 10444M** / free 820M |
| 仓库 | 无未推送提交；工作树仅余并行会话的文档在途改动（磁盘保留，不丢） |
| fnf 四旗标 + br4 录制面 | 激活 conf 内 5 行齐备（HEAD 一致，md5 校验相同） |

## 2. 重启前补齐的能力（本次新做，已提交 `d3fe750`）

| 件 | 作用 |
|----|------|
| `tools/launchd/com.llama-defender.stack.plist`（已安装到 `~/Library/LaunchAgents/`） | **此前无自启项** → 重启后代理/引擎不会自动恢复。RunAtLoad=true（登录即起）、KeepAlive=false（用户主动停栈时不被强拉回） |
| `tools/startup_stack.sh` | 登录后 sleep 20s（刚重启时 Metal 分配易失败）→ `./manage.sh start`（引擎+代理+`CONFIG_AUTOSTART_AUX=embedgemma2` 辅助引擎）→ 拉起监控守护（`monitor_service.sh daemon 300`）→ 落 `logs/startup-stack.log` |
| `tools/post_reboot_check.sh` | 重启后一键验收（8 项）：swap 是否清零 / 代理:4000 / 引擎:8081 / embedgemma2:8083 / fnf 四旗标（读 active.conf）/ br4 录制面 / 监控守护 / `capability_audit` exit 0 / 引擎关键指标 |

## 3. 重启后验收清单

```bash
cd ~/APP/llama.cpp
bash tools/post_reboot_check.sh          # 8 项逐项打勾，FAIL 退出码 1
cat logs/startup-stack.log               # 自启过程（引擎 bootstrap 是否成功）
python3 ../swe-eval/scripts/capability_audit.py   # 对端仪器：exit 0 才可开批
```

判定要点：

- **swap** 应降至 1GB 以内（重启直接释放 10.4GB）——不降则说明系统又长回去了。
- **引擎**:8081 必须在（启动失败时日志见 `not enough free memory to start`，此时**不要再起第二实例**，先看 swap/内存账本）。
- **fnf 四旗标**保持 true（conf 已提交，重启后由 env 继承；无需再戳）。
- **capability_audit** 必须 exit 0（must-have ABSENT 时不得开批；ADR-017）。
- 监控守护在跑且首轮快照 `denied=0`、headroom 回到 18GB+（重启清空 KV/state）。

## 4. 风险与后续

- 重启会终止在跑的 IDE/agent 会话（本仓工作树改动保留在磁盘，但并行会话的未提交上下文会丢）。
- 重启后**先跑验收再开批**；`swap` 是本机内存账本的核心指标，长期 >6GB 即应再评估重启。
- fnf 探针 A/B（swe-eval）以激活戳记 `2026-10-27 00:56:26` 为分段基准，判据见方案件 §4。
