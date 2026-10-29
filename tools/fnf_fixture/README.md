# fnf_fixture — fnf 三旗标机制仪器保真度门（T2，2026-10-29）

**解决什么**：fnf 机制疗效的活体 A/B 读数被模型能力绑架（模型不出循环则机制从未被行使——exp-20261027-fnf-probe-27b 实判 0 真事件）。本工具用**确定性报文回放**独立验证「机制是否按设计工作」，与活体批（疗效问题）正交互补。

## 机制规格（spike 已核，tool_filter.py `_translate_tool_result_errors_ex`）

- fnf「注入」= **出站请求 payload 重写**：代理把 user 消息里 error tool_result 的内容改写为升级提示，再转发后端 → stub 后端捕获的请求体即断言对象。
- 触发：content 含 `File does not exist` / `No such file`（英文标记一次性消费——已改写的中文 `[System: ...` 块不重复计数，天然幂等）；`Wasted call` 与 `InputValidationError` 为旗标无关的固定重写面。
- **F1 梯度**：`_FNF_SEEN[(sid,"")][path]` 跨请求累计；n<3 → 基础提示；**n≥3 → 强提示（「本会话第 N 次…禁止再次读取」）＋ `fnf_guided` 计数**。同路径成功读取 → 计数清零（逃逸口）。
- **F2 候选**：同请求内 ≥2 次成功 Read 的 file_path commonpath（≥2 层目录）= 工作区根证明 → 对失败路径父目录真实 `ls`（2s 超时/512 字符/每 (sid,dir) 缓存）→ 提示附「父目录实况」。无根证明 → 空候选（防会话外路径泄漏）。
- **L 级互斥**（F3' 会话级升级记忆的接缝）：`_LOOP_SESSION_STATE[sid].level>0` 或 L 级注入轮 → fnf 退为基础提示，避免与循环升级双注入。
- 旧 F3（BASH_PATH_KEYS）：独立计数键面，本门实例恒 false。

## 用法

```bash
# 1. stub 后端（:8082，捕获→/tmp/fnf_fixture_captures.jsonl）
python3 tools/fnf_fixture/stub_backend.py &
# 2. 测试代理实例（:4001，独立日志 /tmp/fnf-fixture-proxy.log，零共享状态）
PORT=4001 LLAMA_PORT=8082 LLAMA_BASE_URL="http://127.0.0.1:8082/v1" \
PROXY_LOG_PATH=/tmp/fnf-fixture-proxy.log \
PROXY_FNF_GUIDE_ENABLED=true PROXY_FNF_CANDIDATES_ENABLED=true \
PROXY_LOOP_ESCALATION_MEMORY_ENABLED=true PROXY_LOOP_BASH_PATH_KEYS_ENABLED=false \
python3 anthropic_proxy.py &
# 3. 回放＋断言
python3 tools/fnf_fixture/replay_driver.py   # 5 项断言，非零退出=有 FAIL
# 4. 清理
pkill -f "anthropic_proxy.py" 2>/dev/null; pkill -f stub_backend.py  # 注意：勿误伤生产 :4000（用 pid 精确杀）
```

## 断言面（首跑 5/5 PASS，2026-10-29）

| # | 断言 | 验证的行为 |
|---|---|---|
| A1a | req1 基础提示无禁止语 | F1 馌 1/2 次基础文案 |
| A1b | req2 升级提示（第 3 次＋禁止语） | F1 跨请求账本＋n≥3 梯度＋fnf_guided |
| A2 | 父目录实况含 alpha.py | F2 工作区根证明＋真实 ls 候选 |
| A3 | 清零后回退基础提示 | §1.1 逃逸口 |
| A4 | wasted-call 重写 | 旗标无关固定面 |

代理日志对账面：`Error translation: N tool_result errors rewritten (fnf=X, fnf_guided=Y, fnf_candidates=a/b)`。

## 边界（如实）

- ~~F3' sticky 升级梯度~~ **已落地（phase 2，D/E 组）**：6 连相同 Bash 失败触发 L2（撤工具+警告），紧随无重复轮断言 sticky warning＋fnf 互斥退化。仍开放：L3（force plain-text，需 9 连）与 ctx_recall 连败旁路。
- 响应契约：后端走 **OpenAI chat.completion 形状**（代理 `convert_openai_response_to_anthropic` 消费 `choices[0]`）。
- 生产隔离：:4001/:8082/独立日志三独立；清理时按 pid 精确杀，勿 pkill 误伤 :4000。
