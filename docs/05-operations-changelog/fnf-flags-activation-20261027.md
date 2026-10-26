# fnf 一期四旗标激活戳记（2026-10-27 00:56）

> 落档：把 `logs/ops/fnf-activation-20261027-005605/` 的窗前/窗后工件与戳记固化进版本库
> （`logs/` 已 gitignore，戳记需可对账）。原始工件仍在本机该目录下。

## 1. 激活戳记（STAMP）

| 项 | 值 |
|----|-----|
| 时间 | 2026-10-27 00:56:26 |
| 激活配置 | `configs/splash-qwen36-35b-optiq.conf`（active.conf 指向） |
| conf sha256（改前） | `f498e84960f77d00160bb6b2e32384731e3736c244b11e2006a67f8cb0dde937` |
| conf sha256（改后） | `b56e068382905ffa79fdcb5b6d05b155b6a36dfb31f7158bd71f415ea6fa8d60` |
| 代理 PID | 28461（00:55:05 启动，reload 生效于 00:56:13） |
| 旗标 | F1 + F2 + F3' + F3（全部 `true`） |
| 功能探针 | **PASS**（日志出现 `fnf_guided=`） |

## 2. 激活面（conf 行）

```
export PROXY_FNF_GUIDE_ENABLED=true          # F1  fnf 会话账本＋分级升级提示
export PROXY_FNF_CANDIDATES_ENABLED=true     # F2  最近邻候选（工作区根内 ls）
export PROXY_LOOP_ESCALATION_MEMORY_ENABLED=true   # F3' 会话级升级记忆＋L2 sticky
export PROXY_LOOP_BASH_PATH_KEYS_ENABLED=true      # F3  计数键（v0.1 起兜底面）
```

> **为什么四件同开**：swe-eval 登记册 §五 1026-5 的采纳集＝`PROXY_FNF_GUIDE_ENABLED` /
> `PROXY_FNF_CANDIDATES_ENABLED` / `PROXY_LOOP_ESCALATION_MEMORY_ENABLED`（F1/F2/F3'）。
> 本窗在并行会话已置 F1/F2/旧 F3 的基础上**补齐登记册要求的 F3'**（reloadable 旗标，
> `reload` 即生效，无需重启、不破其窗口）。旧 F3（`PROXY_LOOP_BASH_PATH_KEYS_ENABLED`）
> 按 v0.1 定位为**兜底面**保留同开——它只增加 Bash 变体聚合计数，不改变干预等级语义。

## 3. 激活前后的空窗门（`tools/activate_fnf_flags.sh`）

| 门 | 结果 |
|----|------|
| 进程面（`run_agent.py` / `swe_test exp` / `exp _exec` / `shadow`） | 通过——批跑主进程已收、swe-eval 影子探针（`shadow_probe.py`）在 00:52–00:53 结束 |
| 静默面（代理最近请求距今 ≥ 30s） | 通过（42s） |
| 动作 | conf 幂等置位 → `./manage.sh reload`（SIGHUP，非重启）→ 功能探针 → 窗后快照 |

## 4. 功能验证（生产实测）

| 验证 | 手段 | 结果 |
|------|------|------|
| F1 升级提示 | 新会话 `fnf-verify-<ts>`，同路径 4 次 fnf | 日志 `Error translation: 10 tool_result errors rewritten (fnf=4, fnf_guided=2, fnf_candidates=0/4)` → 第 3、4 次触发升级（符合 §1.3「count≥3」） |
| F2 候选安全边界 | 同上会话**无成功 Read** | `fnf_candidates=0/4` → 工作区根不可证 ⇒ 不取候选（符合 §1.4，无信息外泄） |
| F3' / F3 | 单测 10 例（`test/unit/test_fnf_guide.py`） | 通过；生产侧需循环场景方可观测（待探针批） |

## 5. 判读提示（swe-eval）

- 判读按**本戳记时刻**（2026-10-27 00:56:26）分段；之前的样本属 control 段。
- 判据按方案件 §4 预注册组（主判据 D 面「≥2 次事件占比」76.1%→<40%；Bash 面 fnf/run ↓≥30%；
  无损害组 resolve 不劣化 −5pp / canonical_mismatch 不升 / wall-turns 不升 >10%；
  可达性改验 sticky 语义：treatment 臂饱和会话数下降；F1 计数与 `fnf_guided` 对账一致）。
- 迁移项（S1/S2 触发率、escalate consult、处方 adoption）按分段声明口径披露。

## 6. 回滚

```bash
# 把四旗标置回 false 后 reload 即回到原行为（旗标缺省关＝零行为变化）
sed -i '' 's/^export PROXY_FNF_GUIDE_ENABLED=.*/export PROXY_FNF_GUIDE_ENABLED=false/' configs/splash-qwen36-35b-optiq.conf
# …余三件同理…
./manage.sh reload
```
