# 会话键按 system 分叉（PROXY_SESSION_KEY_SPLIT_BY_SYSTEM，2026-10-09）

> 状态：已提交，**默认关**（需 restart 载码 + conf 开旗标才生效；对生产行为零影响直至开启）

## 1. 事故面（swe6c9a75ff 实证）

批次实例 `swe6c9a75ff-instance_ansible__ansible-d33bedc48fdd93` 出现发送视图锯齿
（~47K ↔ ~59K tokens 逐轮交替，MAIN 轮 TTFT 39-93s）。`tools/trace_replay` 投影 +
archive 消息级 diff 定案根因：

- **同一 claude-cli 进程的两个身份共享静态会话头**。swe-eval run_agent 经
  `ANTHROPIC_CUSTOM_HEADERS` 注入 `X-Claude-Code-Session-Id`（进程级、全请求相同）；
  claude-cli 内建 file-search 子代理（system 明文 "file search specialist… built on
  Anthropic's Claude Agent SDK"，tools=12）与主代理（28K chars，tools=20）在同一
  会话键上 1:1 交替，共享一条单调增长的转录。
- system 每轮不同 ⇒ 前缀从 token 0 断；SUB 轮 hit 0.04-0.11（Splash 保住同身份旧线），
  MAIN 轮 hit 0.57-0.75（每主轮重算 ~150K chars）。两次 epoch 折叠后双双全冷。
- **swe-eval 侧无法修**：会话头是进程级静态注入，无法按调用区分子代理（原方案 A
  "swe-eval 一行修复"前提不成立）。

## 2. 修复（llama.cpp 代理侧，方案 B）

`RequestParser` 单一咽喉点：开启旗标时 `ctx.session_id += "::sys" + sha256(system)[:8]`。
每身份确定性独立 key → 下游 engine canonical/台账/manifest/archive/R-CM 守卫**全部
自然一致分裂**，各身份独立缓存线（回 99% 档）。

- 旗标：`PROXY_SESSION_KEY_SPLIT_BY_SYSTEM`（bool，reloadable，**默认 false**）
- 代码：`pipeline.py:_split_session_key_by_system`（fail-open：无 system/已带后缀不改写）
- 测试：`test/unit/test_session_split.py`（6 例：关旗标不分裂/确定性/异 system 分叉/
  block 列表/无 system/防双后缀）
- 副作用说明：分叉后 ledger dup 计数按身份独立（SUB 的重读不再喂 MAIN 的 dup 计数——
  语义更正确）；`/api/sessions` 键数量约 ×2；代理日志 `sess=` 仍显示原 8 字符头（仅
  ctx 级 key 分叉）

## 3. 启用（下次 restart 窗口）

```bash
# conf 追加（两个 splash conf 同步，防切 conf 丢失）
export PROXY_SESSION_KEY_SPLIT_BY_SYSTEM="true"
./manage.sh restart   # 载码 + 读旗标（restart 前旗标改动 reload 不生效：代码未载）
```

## 4. 附带修复

`tools/trace_requirements.py` 扫描器被 `test/unit/__pycache__/*.pyc`（Python 3.14 字节
码，外部跑测试残留）炸出 UnicodeDecodeError → 改为按 `.py/.sh` 扩展名过滤 +
`errors="replace"` 容错。
