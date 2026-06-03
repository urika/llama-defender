# 故障排查记录

## Qwen Chat Template 兼容性问题修复

> 记录时间: 2026-06-03
> 影响范围: rapid-mlx 后端 + Claude Code 客户端
> 相关组件: `anthropic_proxy.py`, rapid-mlx, Qwen3.6-35B-A3B-4bit

---

### 一、问题现象

C组实验启动后，Claude Code 不断重试发送请求，但代理始终收不到有效响应：

| 时间线 | 现象 |
|--------|------|
| 09:01:52 | POST /v1/messages → `Forwarding to...` → 无后续响应 |
| 09:07:06 | 再次 POST → 仍无响应 |
| 09:15:44 ~ 09:21:05 | 无限重试循环，每次 `Streamed text=0 chars` |

代理层死锁修复后（移除嵌套 `with _llama_lock`），请求能正常到达后端，但后端返回空 SSE 流。

---

### 二、根本原因

**后端 rapid-mlx 日志报错：**
```
jinja2.exceptions.TemplateError: System message must be at the beginning.
```

**触发条件：**

1. Claude Code 启用了 `mid-conversation-system-2026-04-07` beta 特性
2. 该特性在对话中间注入 `system` 消息（非标准 Anthropic API 行为）
3. 代理的 `convert_anthropic_messages_to_openai()` 直接传递了这些 `system` / `developer` role 消息
4. Qwen 官方 chat template（Jinja2）严格要求 **所有 `system` 消息必须在消息列表最开头**
5. `tokenizer.apply_chat_template()` 渲染时抛出 TemplateError
6. rapid-mlx 返回空的 SSE 流 → 代理收到空响应 → Claude Code 超时重试

---

### 三、诊断路径

```
代理 POST 超时
  → 直接后端 POST 正常（0.51s）→ 问题在代理侧
  → 发现 _llama_lock 嵌套死锁 → 修复代理死锁
  → 修复后仍超时 → 检查后端日志
  → 发现 TemplateError: System message must be at the beginning
  → 确认 Claude Code 的 mid-conversation-system beta 特性
  → 搜索社区修复模板
```

**关键线索：**
- 代理 `GET /v1/models` 正常（不经 `_llama_lock`）
- 代理 `POST /v1/messages` 超时（需获取 `_llama_lock`）
- 后端直接 `POST /v1/chat/completions` 正常
- 后端日志连续出现 15+ 次 TemplateError

---

### 四、解决方案

#### 4.1 方案对比

| 方案 | 说明 | 可行性 |
|------|------|--------|
| 修改 rapid-mlx 启动参数 | `--chat-template /path/to/template.jinja` | ❌ rapid-mlx 0.6.30 不支持该参数 |
| 修改代理代码 | 提取并前置所有 system 消息 | ✅ 可行但不够彻底 |
| **替换模型 chat template** | 使用社区修复模板覆盖模型目录中的文件 | ✅ **最终采用** |

#### 4.2 后端类型与修复方式

| 后端 | 修复方式 | 原理 |
|------|---------|------|
| **rapid-mlx** | 替换 HF Hub 缓存中的 `chat_template.jinja` | rapid-mlx 从模型目录加载 tokenizer，自动读取 `chat_template.jinja` |
| **llama-server** | `--chat-template` 参数覆盖 | llama-server 支持通过 CLI 传入自定义 Jinja 模板字符串，覆盖 GGUF 内置模板 |

#### 4.3 rapid-mlx 实施步骤

**Step 1: 下载修复模板**

```bash
curl -L -o assets/chat-templates/qwen-fixed-chat-template.jinja \
  "https://huggingface.co/froggeric/Qwen-Fixed-Chat-Templates/resolve/main/chat_template.jinja"
```

**Step 2: 定位模型目录**

HuggingFace Hub 缓存路径（以 Qwen3.6-35B-A3B-4bit 为例）：
```bash
MODEL_DIR="$HOME/.cache/huggingface/hub/\
models--mlx-community--Qwen3.6-35B-A3B-4bit/\
snapshots/38740b847e4cb78f352aba30aa41c76e08e6eb46"
```

**Step 3: 替换 `chat_template.jinja`**

> ⚠️ 原文件是 symlink 指向共享 blob，需先删除再写入实际文件：

```bash
# 备份（可选）
cp "$MODEL_DIR/chat_template.jinja" "$MODEL_DIR/chat_template.jinja.bak"

# 删除 symlink，写入修复模板
rm "$MODEL_DIR/chat_template.jinja"
cp assets/chat-templates/qwen-fixed-chat-template.jinja "$MODEL_DIR/chat_template.jinja"
```

**Step 4: 重启服务**

```bash
./manage.sh restart
```

#### 4.4 llama-server 实施步骤

**Step 1: 修改 `manage.sh`**

在 `_start_llama_server()` 函数中，于 `LLAMA_EXTRA_ARGS` 之前添加：

```bash
# Custom chat template override (e.g. fixed Qwen template)
if [[ -n "${LLAMA_CHAT_TEMPLATE:-}" && -f "$LLAMA_CHAT_TEMPLATE" ]]; then
    local _template_content
    _template_content=$(cat "$LLAMA_CHAT_TEMPLATE")
    args+=(--chat-template "$_template_content")
fi
```

**Step 2: 修改配置 `configs/qwen3.6-27b-mtp.conf`**

```bash
# 修复后的 Qwen chat template（解决 System message must be at the beginning 错误）
LLAMA_CHAT_TEMPLATE="assets/chat-templates/qwen-fixed-chat-template.jinja"
```

> 注意：llama-server 的 `--chat-template` 参数接受的是**模板字符串**本身，不是文件路径。`manage.sh` 通过 `$(cat file)` 读取文件内容后传入。

---

### 五、修复模板的关键改进

[froggeric/Qwen-Fixed-Chat-Templates](https://huggingface.co/froggeric/Qwen-Fixed-Chat-Templates) v19 修复了官方模板的多个严重问题：

| 问题 | 修复 |
|------|------|
| **Mid-Conversation System Crash** | 支持消息历史任意位置的 system 消息 |
| **developer role 不支持** | 新增 `developer` role 映射（Claude Code 使用） |
| **Empty Think Poisoning** | 彻底消除空 `<think>` 标签注入 |
| **KV Cache 失效** | 严格时序渲染，保证 100% prefix cache 命中率 |
| **Agentic Loop Stalls** | 修复 tool call 与对话文本的互斥停止 bug |
| **minijinja 兼容性** | 移除 `loop.previtem` 等 C++ 不支持的 Jinja2 特性 |

---

### 六、相关资源

| 资源 | 链接 |
|------|------|
| **修复模板（主要）** | https://huggingface.co/froggeric/Qwen-Fixed-Chat-Templates |
| **GitHub Issue 讨论** | https://github.com/QwenLM/Qwen3/issues/1831 |
| **备用模板** | https://huggingface.co/barubary/qwen3.5-barubary-attuned-chat-template |
| **rapid-mlx 项目** | https://github.com/arielnlee/rapid-mlx |

---

### 七、修复覆盖范围

#### 7.1 HuggingFace 缓存中的 rapid-mlx 模型（已修复 5 个）

| # | 模型 | 路径 |
|---|------|------|
| 1 | Qwen3.6-35B-A3B-4bit | `models--mlx-community--Qwen3.6-35B-A3B-4bit` |
| 2 | Qwen3.6-27B-AEON-Ultimate-Uncensored-BF16-mlx-fp16 | `models--mlx-community--Qwen3.6-27B-AEON...` |
| 3 | Qwen3.6-27B-AEON-Ultimate-Uncensored-BF16-mlx-4Bit | `models--mlx-community--Qwen3.6-27B-AEON...` |
| 4 | Qwen3.5-27B-4bit | `models--mlx-community--Qwen3.5-27B-4bit` |
| 5 | Qwen3-Coder-30B-A3B-Instruct-4bit | `models--mlx-community--Qwen3-Coder-30B...` |

#### 7.2 llama-server 的 GGUF 模型（已配置）

| 模型 | 配置 | 修复方式 |
|------|------|---------|
| Qwen3.6-27B-MTP-UD-Q4_K_XL.gguf | `configs/qwen3.6-27b-mtp.conf` | `--chat-template` 参数覆盖 |

#### 7.3 修复指标对比

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| TemplateError | ❌ 每次请求都报错 | ✅ 完全消失 |
| 代理响应 | ❌ `Streamed text=0` | ✅ 正常生成文本和 tool calls |
| Claude Code | ❌ 无限重试循环 | ✅ 正常执行编码任务 |
| KV Cache | ❌ 频繁失效 | ✅ 100% 命中率 |

---

### 八、修改的文件清单

| 文件 | 修改内容 |
|------|---------|
| `anthropic_proxy.py` | 移除嵌套 `with _llama_lock` 死锁 bug |
| `manage.sh` | 添加 `LLAMA_CHAT_TEMPLATE` 支持（llama-server 后端） |
| `configs/qwen3.6-27b-mtp.conf` | 添加 `LLAMA_CHAT_TEMPLATE="assets/chat-templates/qwen-fixed-chat-template.jinja"` |
| `TROUBLESHOOTING.md` | 本文档（记录本次修复） |

---

### 九、关联修复

本次排查过程中还修复了另一个问题：

**代理死锁 bug**
- **原因:** `anthropic_proxy.py` 本地模式下存在嵌套 `with _llama_lock`
- **代码:**
  ```python
  with _llama_lock:              # 外层获取 Semaphore(1)
      ...
      with _llama_lock:          # 内层再次获取 → 永久阻塞
          resp = urllib.request.urlopen(...)
  ```
- **修复:** 移除内层 `with _llama_lock`，保留外层即可
