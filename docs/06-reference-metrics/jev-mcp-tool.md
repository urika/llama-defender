# Jev MCP 工具

`tools/jev_mcp_server.py` 将 Command Code 的 `typesafe/jev` 决策 API 暴露为本地 stdio MCP 工具 `jev_decide`。它接受 `state` 和命名的 `questions`，支持 `noul`、`choice`、`score`，返回原始概率结构，不生成自由文本结论。自 **v1.1.0（2026-10-30）** 起带**证据契约**（问题可声明 `requires_evidence`，state 缺证则契约弃权、不发模型）、每题 `answer_class` 判读标注与 `provenance` 溯源——见下文 [§证据契约与判读语义（v1.1）](#证据契约与判读语义v11)。

## 安全设置 API key

先在当前用户的 shell 环境中设置 Command Code API key；不要把真实 key 写进仓库文件、`.mcp.json`、命令行参数或 MCP server 源码：

```bash
export CMD_API_KEY='从 Command Code Studio 创建的 API key'
```

MCP server 启动时从 `CMD_API_KEY` 读取密钥，并以 HTTPS Bearer token 调用：

```text
https://api.commandcode.ai/provider/v1/systemone
```

API key 对应的账号/计划必须有 Provider API 访问权限（GOAT 或以上的支持计划，或 Provider plan）。

## 注册到 Command Code

在安装/启动 Command Code 的同一 shell 中运行：

```bash
cmd mcp add jev -- python3 /Users/<用户名>/APP/llama.cpp/tools/jev_mcp_server.py
```

将路径替换为该文件的实际绝对路径。确认该 shell 环境已导出 `CMD_API_KEY`，然后在 Command Code 中打开 `/mcp`，检查 `jev` 已连接且工具 `jev_decide` 可用。

如果 Command Code 从 GUI 启动、未继承 shell 环境变量，请从已导出变量的 shell 启动它；不要把 key 写到被版本控制的 MCP 配置。仓库 stdio server 不读取 `configs/secret.local.conf`，以免将 llama-defender 云端凭证与 Jev API 凭证混用。

## 示例

在会话中请求：“用 Jev 判断这段状态是否紧急，并返回概率：支付连续失败三天。”工具输入等价于：

```json
{
  "state": "Payments failed for three days.",
  "questions": {
    "urgent": {
      "type": "noul",
      "instructions": "Does this need urgent attention?"
    }
  }
}
```

该决策调用需 GOAT 或以上计划；输入按 Provider API 费率计费，输出 token 免费。模型结论是概率信号，调用方应自行设定阈值并保留业务校验。

## 证据契约与判读语义（v1.1，2026-10-30）

> 依据：jev 质量门正负对照校准（swe-eval 6678a14）暴露的两个接口契约缺口——
> 「接口允许无证据作答」（答案依赖的证据不在 state 里，模型只能按参数记忆作答，
> 正负对照不分离）与「噪声级输出可被记录为答案」（conf 0.19/0.34 的读数被记成
> high 与 hit）。本节契约与 rsi-jev 级联网关 v0.2.0 同语义（实现为两份，勿单方改）。

**① `requires_evidence`（证据契约）**：问题可声明答案依赖的证据类列表——

```json
{
  "state": {
    "test_source": "cm = ConfigManager(conffile='/tmp/x.cfg')",
    "base_commit_api": {"ConfigManager.__init__": "(self, conf_file=None, defs_file=None)"}
  },
  "questions": {
    "api_real": {
      "type": "noul",
      "instructions": "Is the kwarg name 'conffile' valid for this signature?",
      "requires_evidence": ["base_commit_api"]
    }
  }
}
```

存在性判定（机械、保守）：dict → 点路径存在（`a.b`），路径断裂退化为全包子串；
str → 子串；list → 任一元素。**state 缺任一声明类 → 该题契约弃权：
`answer_class=missing_evidence`、`source=contract`、无任何概率读数，不发 HTTP。**
无证据的判读只能来自模型参数记忆——实测证据入 state 后同型问题本地 4B 即可
p=0.957 正确判读，所以**调用方的修法是补 state，不是换模型**。

**② `answer_class`（每题判读标注）**：`answer`（可直接采纳）｜`missing_evidence`
（契约弃权）。官方直连面没有本地分布的校准阈值，故不标 `below_threshold`——
阈值判断仍是调用方的责任；级联面（`mcp__jev-cascade__*`）才有完整的五档。
**只有 `answer` 可计入命中/自动通过统计。**

**③ `provenance`（溯源）**：每个响应附 `{server, model, ts, state_sha256}`。
落证（写入实验记录/判定文档）时应**原样持久化**，不得转写为散文——
手写「route=local（rsi-jev-v6.1-vl-4b）」这类散文溯源正是 6678a14 校准记录
事后无法机器复核的原因之一。

## 使用策略与校准核验（2026-10-13 调研沉淀）

> 背景：Jev 泛化能力与开源替代调研。核心事实——TypeSafe 的 RLCD 校准声明只在其自有
> 分布上成立，**不覆盖本仓的判定数据**；开源替代（Laya / Kev / Ollaya / WaterSheep 等）
> 的真实差距在训练集多样性与校准证据，而非接口兼容性。
>
> **级联流程设计**（规则 → Jev → 分层升级 + 抽审计）见
> [`docs/02-architecture-design/decision-cascade-rules-jev-escalation-20261013.md`](../02-architecture-design/decision-cascade-rules-jev-escalation-20261013.md)
> ——含业界实践综述（scanners-before-classifiers、cascade 双阈值、延迟预算）与
> swe-eval / agent_go 跨仓落地提案。
>
> **外部调研交叉分析**（RSI-Jev 递归自我改进范式、8 条训练原则、官方四大
> 架构模式、免训练路线 LLM2Jev、证据等级标注）见
> [`docs/06-reference-metrics/jev-research-supplement-20261013.md`](jev-research-supplement-20261013.md)。

- **现有姿势已对一半**：`jev_decide` 返回原始概率、调用方自定阈值，是泛化策略的正确
  一半；另一半是**在本仓自己的判定数据上定期核验校准**（方法参考 Vals AI / Convex
  决策榜：固定判定集 + 重复多次 + 洗牌选项 + 准确率/校准误差分开报），因为 TypeSafe
  的校准声明不覆盖我们的分布。
- **准确率不足时的处置顺序**：优先 **L0——改 criteria / 拆问题 / 过滤无关 state**
  （distractor 是官方承认的失败源，多步推理链应拆成多次单步判定）；其次
  **L1——用少量标注做阈值重校准**（temperature scaling、按覆盖-准确率曲线定阈值、
  不确定样本升级人工/大模型）；**不必也不宜自训模型**——L2/L3（LoRA/头训练、
  重预训练/RL）是模型厂商的事，场景内判据工程与校准才是使用方的杠杆。
- **降外部 API 依赖时的选型标准**：选开源替代（`/v1/systemone` 兼容层，Ollaya 换
  `TYPESAFE_BASE_URL` 即切、Kev/WaterSheep/jev-style SDK 直连）看的不是"接口兼容"
  ——接口层可零成本切换；要看**它的训练集多样性与项目自己公布的校准证据**
  （多项目 README 自认泛化/校准落后于托管 Jev，如 Decima 0.616、jev-style
  “hosted Jev 更准”、jevos 0.815 vs 0.927）。

## 额度与限流（2026-09-27 落地）

jev 与运营 CLI 会话**共享同一账户的两个滚动窗口**（5 小时／周，按 credit 计）：窗口被吃满时
决策调用会被 `HTTP 429` 拒绝。官方 provider 端点没有额度接口，故本仓自建两件：

**① MCP 工具 `jev_usage`**（只读元数据：不发内容、不消耗判定、实测不计次不计费）——
返回两窗 `used`/`cap`/`resetAt`、剩余额度与计划，用于「调用前先看能不能调」。
**② 429 错误体白名单提取**——官方语义里拒绝消息会**点名窗口并给出重置时刻**；旧实现
把响应体整个丢弃，现改为只取 `error.message`（截断＋脱敏，绝不放行整包、绝不回显密钥），
错误串因此变成可行动的「窗口 + 重置时刻」。**超额时不要盲退避**：先读 `resetAt` 再等，
或 `/extra` 买额度、`/upgrade` 升档。

命令行等价物：`python3 tools/jev_quota.py [--json] [--plan]`，退出码分级
`0` 充足 ／ `3` 接近（≥80%）／ `4` 超额 ／ `5` **不可探测**——4 与 5 严格区分，
**探测失败不得被误读成额度耗尽**。

> 端点、实测台账（账户快照、单次调用 0.0000366 credit、元数据 GET 不计费）与落地细节见
> `docs/06-reference-metrics/jev-quota.md`。`/alpha/*` 属 CLI 内部端点、**未文档化**，
> 故实现按「不可探测」降级而非硬依赖。

## 部署形态与本地服务解耦（2026-10-20 核实）

**结论：`jev` MCP 是纯云端代理，与本地 `rsi-jev`（`:8022`）没有任何依赖关系。**
停掉本地服务不影响 `jev_decide` / `jev_usage`。

### 调用链

```
jev MCP (stdio)  ──HTTPS + Bearer──>  https://api.commandcode.ai/provider/v1/systemone
                                       model: typesafe/jev
                                       key:   CMD_API_KEY（环境变量）
```

`tools/jev_mcp_server.py` 中**没有任何 `8022` 引用**，也没有本地降级分支
（`grep -c '8022' tools/jev_mcp_server.py` → `0`）。

### 三个易混淆的组件

| 组件 | 地址 | 是否依赖本地 :8022 | 说明 |
|------|------|--------------------|------|
| **`jev` MCP** | stdio | ❌ 否 | 直连官方云端；本项目级配置在 `~/APP/llama.cpp/.opencode/opencode.json` |
| `jev-cascade` 网关 | `:8023` | ✅ 是 | 本地优先 → 置信度低才升级官方；本地不可用则整体降级官方 |
| `rsi-jev` 本地模型 | `:8022` | — | 本地 MLX 推理（`v6.1-vl-4b-mlx-8bit`），零外发 |

> 也就是说：**只用 `jev` MCP 时，`:8022` 与 `:8023` 都不是必需的**。
> 只有需要「本地优先、零外发」的 cascade 路径时才需要它们。

### `rsi-jev`（`:8022`）生命周期管理

由 launchd 常驻管理，plist 位于
`~/Library/LaunchAgents/com.agentgo.rsi-jev.plist`，关键项：

```xml
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>   <!-- 进程退出会被自动拉起 -->
```

**因为 `KeepAlive=true`，`kill` 与 `launchctl stop` 都只是「假停」**——会被立刻重启
（实测 `launchctl stop` 后 pid 变化、`runs` 自增）。真正的启停只有以下几种：

#### 项目自带脚本（日常）

```bash
cd ~/APP/rsi-jev
./tools/start-service.sh {start|stop|restart|status|logs}
./start-all.sh          # 一键：8022 本地 + 8023 网关
```

> `start-service.sh start` 用 `nohup` 直接拉起，**可绕过 launchd 的 disable 状态**——
> 排查「为什么又起来了」时先想到它。

#### launchctl（真正生效）

```bash
UID_=$(id -u)

# 临时停（本次登录内；KeepAlive 不会拉起）
launchctl bootout gui/$UID_/com.agentgo.rsi-jev

# 重新启用
launchctl bootstrap gui/$UID_ ~/Library/LaunchAgents/com.agentgo.rsi-jev.plist

# 跨登录持久禁用（重启后也不启动）
launchctl disable gui/$UID_/com.agentgo.rsi-jev
launchctl bootout gui/$UID_/com.agentgo.rsi-jev

# 解除持久禁用
launchctl enable    gui/$UID_/com.agentgo.rsi-jev
launchctl bootstrap gui/$UID_ ~/Library/LaunchAgents/com.agentgo.rsi-jev.plist
```

`launchctl disable` 会写入 `/var/db/com.apple.xpc.launchd/disabled.<uid>.plist`，
可用 `launchctl print-disabled gui/$UID_` 核对。

#### 相关附属服务

| 服务 | 作用 | 备注 |
|------|------|------|
| `com.agentgo.jev-cascade` | `:8023` 级联网关 | `KeepAlive=true`；本地不可用时降级官方 |
| `com.agentgo.jev-keepalive` | 每 300s `curl /health` | **只探活保温，不会拉起服务** |

`keepalive.sh` 的目的是防止 MLX 权重空闲被换出（首次调用冷启约 2.4s），
脚本内只有两条 `curl`，无任何 `launchctl`/启动调用，因此 `bootout` 后不会被它拉回。

### 当前状态（2026-10-20）

本机为省内存已**跨登录持久禁用** `:8022`：

```
launchctl print-disabled gui/501  →  "com.agentgo.rsi-jev" => true
```

实测收益与影响：

| 项 | 禁用前 | 禁用后 |
|---|---|---|
| rsi-jev footprint | 8.27 GB | 0 |
| 系统 free | 0.1 GB | 8.1 GB |
| 系统 compressor | 11.7 GB | 3.4 GB |
| `jev_decide`（云端） | 正常 | **正常**（实测 `noul=0.97`） |
| `jev-cascade` `:8023` | local ok | `local.ok=false`（仅降级官方） |

> 该禁用与
> [`../04-analysis-diagnostics/ornith-9b-memory-pressure-20261020.md`](../04-analysis-diagnostics/ornith-9b-memory-pressure-20261020.md)
> 属同一次内存治理：48 GB 机器上同时跑 Splash 27B（15–19 GB）+ rsi-jev（8.27 GB）+ 9B（5.7 GB）
> 会触发全局换页，拖慢所有引擎。
