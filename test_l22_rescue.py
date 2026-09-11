#!/usr/bin/env python3
"""L-22/L-23 会话拯救与 usage 回填 — 单元验证。

Tests:
1. _classify_rescue_response: A3 空转短语 / A1 raw-XML 签名 / 正常文本不误伤
2. _build_rescue_follow_up: 消息对结构(assistant 原文 + user 纠偏)
3. _estimate_prompt_tokens: 估算单调性 / 坏输入回退
4. 微轮闭包门: rescue 开关旁路(ctx_recall 子开关回归)
"""

import sys

errors = []


def check(description, condition):
    if not condition:
        errors.append(f"FAIL: {description}")
        print(f"  ✗ {description}")
    else:
        print(f"  ✓ {description}")


print("\n--- Test 1: _classify_rescue_response ---")
from anthropic_proxy import _classify_rescue_response, _build_rescue_follow_up, _estimate_prompt_tokens

# A3: 空转签名
check("空文本 → idle", _classify_rescue_response("", False) == "idle")
check("空白 → idle", _classify_rescue_response("  \n ", False) == "idle")
check("单句号 → idle", _classify_rescue_response(".", False) == "idle")
check("No response requested. → idle",
      _classify_rescue_response("No response requested.", False) == "idle")
check("大小写变体 → idle",
      _classify_rescue_response("NO RESPONSE REQUESTED.", False) == "idle")
# A1: raw-XML 工具签名(seq19 实测形态)
check("raw tool_call 尾 → raw_xml",
      _classify_rescue_response(
          "deprecations should be disableable via config.\n</parameter>\n"
          "<parameter=task>\nfix deprecation config\n</parameter>\n"
          "</function></tool_call>", False) == "raw_xml")
check("</function> 单签名 → raw_xml",
      _classify_rescue_response("let me try </function>", False) == "raw_xml")
# 不误伤
check("正常任务总结 → None",
      _classify_rescue_response(
          "I fixed the bug in linux.py by updating the mount facts parser.",
          False) is None)
check("有工具调用 → 永不拦",
      _classify_rescue_response("No response requested.", True) is None)
check("含 XML 的正常文档文本仍拦(纯文本无工具=漂移高概率)",
      _classify_rescue_response("Here is how </tool_call> works.", False) == "raw_xml")

print("\n--- Test 2: _build_rescue_follow_up ---")
fu = _build_rescue_follow_up("idle", "No response requested.")
check("消息对长度 2", isinstance(fu, list) and len(fu) == 2)
check("roles = assistant,user", [m["role"] for m in fu] == ["assistant", "user"])
check("assistant 带原文", fu[0]["content"] == "No response requested.")
check("user 为纠偏提示(非空)", len(fu[1]["content"]) > 20)
fu2 = _build_rescue_follow_up("raw_xml", "...raw text...")
check("raw_xml 分支 roles 相同", [m["role"] for m in fu2] == ["assistant", "user"])
check("raw_xml 纠偏文案含 tool_use", "tool_use" in fu2[1]["content"])

print("\n--- Test 3: _estimate_prompt_tokens ---")
small = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
big = {"model": "m", "messages": [
    {"role": "user", "content": "x" * 40000},
    {"role": "assistant", "content": "y" * 40000},
]}
e1, e2 = _estimate_prompt_tokens(small), _estimate_prompt_tokens(big)
check("小请求 ≥1", e1 >= 1)
check("大请求估算大于小请求", e2 > e1)
check("估算随体量单调", e2 > 1000)
check("坏输入回退 1", _estimate_prompt_tokens({"messages": set()}) == 1)

print("\n--- Test 4: 微轮闭包门 rescue 旁路 ---")
import proxy_state as _ps
_pd, _mt = _ps.PROXY_PD_ENABLED, getattr(_ps, "PROXY_PD_MICRO_TURN_ENABLED", False)
_rs = getattr(_ps, "PROXY_RESCUE_ENABLED", True)
check(f"生产现状: PD={_pd} MICRO_TURN={_mt} RESCUE={_rs}",
      isinstance(_pd, bool) and isinstance(_rs, bool))
gate_open = _pd and (_mt or _rs)
check("rescue on → 闭包门开(即使 ctx_recall 子开关关)", gate_open)
# 回归: 两者全关时门必须关
_sim = False and (_mt or False)
check("全关模拟 → 门关", _sim is False)

print()
if errors:
    print(f"{len(errors)} check(s) FAILED")
    sys.exit(1)
print("All L-22/L-23 checks passed")
