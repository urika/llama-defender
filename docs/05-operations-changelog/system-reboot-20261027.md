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

## 4. 重启后实测结果（05:54–05:56）

| 项 | 重启前 | 重启后 | 判定 |
|----|--------|--------|------|
| swap | 10.44GB / 11.26GB | **0.00M** | ✅ 清零（重启的主要收益） |
| 代理 :4000 | pid 39619 | pid **5196** healthy | ✅ |
| 引擎 :8081 | Splash 1.4.0 35B-A3B | 存活，pressure=**normal** | ✅ |
| embedgemma2 :8083 | 运行中 | 运行中（pid 5390） | ✅ |
| fnf 四旗标 | 全 true（conf） | 全 true（重启后由 env 继承） | ✅ |
| br4 录制面 | true | true（近 10 分钟 4 个归档文件） | ✅ |
| capability_audit | exit 0 | exit 0，**`fnf_guide_kit` 首次 PRESENT** | ✅ |
| 监控守护 | 运行中（手工 nohup） | **PID 6947，由 launchd 托管（KeepAlive=true）** | ✅（首次开机自启即失败，DEF-323 已修，见下） |

> **重启暴露的三个自启缺陷（已当场修复，见 DEF-323）**：① launchd 登录环境 PATH 不含
> `/opt/homebrew/bin` → `manage.sh start` 找不到 splash（rc=1，引擎未自启，最终由登录后
> 其他会话拉起）；② 守护由 wrapper 内 `nohup &` 拉起 → launchd 作业退出时**连坐杀掉作业组
> 子进程**（守护 PID 2726 即死）→ 拆出独立 launchd 作业 `com.llama-defender.monitor`
> （KeepAlive=true）；③ 守护循环单周期异常可终止守护 → 加 `set +e` 包裹与单步失败保护。

## 5. 第二次重启（2026-10-28 22:55）——swap 再次耗尽，同一治理面复发

| 项 | 重启前 | 重启后 |
|----|--------|--------|
| swap | 11696MB / 12288MB（95%，自 10-27 21:29 越阈持续 CRIT 约 25h） | **0.56M**（动态 swap 收缩到 1024M 池） |
| 在飞任务 | `run_agent.py`（swef12d2，127 turns、96K）+ `shadow_probe.py`（exp-20261027-guards-validation） | **被重启打断**，23:17 栈恢复后由对端自动续跑（swef12d2 93–96K 上下文请求在飞） |
| 分段边界 | — | **第三个边界**（重启后代理 PID/引擎计数器/KV/state 全部归零；重启前后样本不可直接比） |

> **本次重启暴露并修复 DEF-324**：launchd 守栈作业 wrapper 退出即连坐杀掉整个栈
> （22:56 自启成功 → wrapper 22:57:18 退出 → 23:17 栈全灭）。修法＝wrapper 常驻前台守栈
> ＋ `KeepAlive={"SuccessfulExit":false}`（异常退出才重启、人工 stop 不拉起）。
> 验收：守栈 PID 5063，90s 后栈三端口俱在，`post_reboot_check.sh` 9 项全绿。
> 注：昨天那次没暴露同款问题，只因 `manage.sh start` 当时因 PATH 缺 Homebrew 直接失败，
> 栈不是 launchd 起的（父进程非 launchd）→ 躲过连坐（**故障被掩盖而非不存在**）。

## 6. 风险与后续

- 重启会终止在跑的 IDE/agent 会话（本仓工作树改动保留在磁盘，但并行会话的未提交上下文会丢）。
- 重启后**先跑验收再开批**；`swap` 是本机内存账本的核心指标，长期 >6GB 即应再评估重启。
- fnf 探针 A/B（swe-eval）以激活戳记 `2026-10-27 00:56:26` 为分段基准，判据见方案件 §4；
  **本次重启构成第二个分段边界**（代理 PID 5196、引擎计数器与 KV/state 全部归零，
  重启前后样本不可直接比——沿「按代理重启分段」先例披露）。
