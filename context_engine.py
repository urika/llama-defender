#!/usr/bin/env python3
"""context_engine.py — 上下文工程 Phase 1 核心引擎（R8.1-R8.3，stdlib only）。

设计依据: docs/02-architecture-design/llama-defender-context-engineering-design.md §4.3/§4.9/§11/§12
  - 前缀纪律（append-only）: canonical 历史一经写入永不改写——每轮只对
    客户端新到达的 observation 做一次写入期压缩并冻结（§4.3），历史区
    不再回溯重写（旧 ContentCompressor 的每轮改写 = 前缀缓存击穿元凶，
    Phase 0 §12.3 结论 4 已坐实）。
  - epoch 状态机（§4.9）: 发送前检查 total > S（默认 min(65%×ctx, 70K) tokens,
    35B 校准: 受 pflash 96K 阈值与 epoch 轮 P90<60s 门禁约束）→ 超限触发
    一次性收编: K 窗口外轮次折叠入压缩区（台账格式动作行+句柄），原生流
    重切为最近 K 轮。**S 为真实 prompt_tokens 口径**（2026-08-22 校准:
    est 低估 1.6×, 触发信号取后端 usage 回填, 见 EST_REAL_RATIO）。
    epoch 后公共前缀止于 L0（system+tools），缓存重建
    范围 = 压缩区+原生流（有界的每-epoch-一次成本，摊销进多轮）。
  - 回退保护: 压缩后仍超限 → K=4 收紧 → L3 行数减半 → 仍超限返回错误
    （代理无权把历史压到失真假装放得下，超长会话该结束的是任务）。

线程安全: 单例 EngineStore, 内部 Lock; 会话 TTL/FIFO 有界（复用 ledger 模式）。
"""
import re
import threading
import time
from datetime import datetime

import proxy_state as _ps
import unit_model as _um

DEFAULT_EPOCH_TRIGGER_TOKENS = 70000   # S: 真实 prompt_tokens 口径(2026-08-22 校准)
DEFAULT_WINDOW_K = 24                  # K: epoch 重切时保留的最近轮数(§4.4)
TOKEN_CHAR_RATIO = 4                   # 仓库统一估算口径: 1 token ≈ 4 chars
MIN_RESULT_KEEP_CHARS = 512            # §4.3 最小阈值: 小结果逐字保留不包装
# est(/4 口径)→真实 tokens 校准系数(实测 82346/51248=1.61, 2026-08-22 42355d18
# 重跑后端 adaptive_prefill prompt= 与引擎 est 对比)。est 口径触发会迟到 1.6×:
# est 60K = 真实 ~96K 恰贴 pflash 阈值——本次重跑系统在真实 82K 崩溃, est 触发点
# 永远到不了。故 epoch 触发与容量判定一律用真实口径(后端 usage 回填优先,
# 无回填时 est×本系数换算)。
EST_REAL_RATIO = 1.6


class ContextOverflowError(Exception):
    """§4.9 回退保护硬上限: 收紧(K=4)+减半后仍超预算——会话该结束,而非失真压缩。"""

# 工具类别 → (保留模板: head/tail 预算 chars, 句柄键)
_TOOL_BUDGETS = [
    (("search", "websearch", "query"), ("search", 6000)),
    (("read", "glob", "grep", "ls", "notebookedit"), ("file", 8000)),
    (("fetch", "curl", "download", "scrape"), ("http", 8000)),
    (("bash", "shell", "exec", "command"), ("command", 6000)),
]
_ERROR_MARKERS = ("error", "traceback", "exception", "failed", "fatal")

# _HANDLE_KEYS 已统一至 unit_model.HANDLE_KEYS(词汇表对齐,2026-08-29)


def estimate_tokens(chars):
    """chars → token 估算(仓库统一 /4 口径)。"""
    return (int(chars) + TOKEN_CHAR_RATIO - 1) // TOKEN_CHAR_RATIO


def _classify_tool(tool_name):
    tool = (tool_name or "").lower()
    for keys, spec in _TOOL_BUDGETS:
        if any(k in tool for k in keys):
            return spec
    return ("generic", 6000)


# _extract_handle 已统一至 unit_model.extract_handle(结构化 Handle 形态);
# 本模块展示串场景经 _um.handle_value() 转换。


def _result_text(block):
    """tool_result 文本——已迁移 unit_model.result_text(词汇表统一);保留薄委托(测试引用)。"""
    return _um.result_text(block)


def compress_observation(tool_name, args, text, budget=None):
    """写入期压缩单条 tool_result 文本 → (compressed, handle, kind)。

    规则（§4.3）:
    - 错误/异常全文保留（负反馈是稀缺信号——仅 cap 到 budget×2 防失控）
    - 原文 ≤ budget 或 ≤ MIN_RESULT_KEEP_CHARS: 逐字保留（不包装——
      小结果包装后反而更长）
    - 超限: head+tail 各半 + 句柄行
    """
    kind, default_budget = _classify_tool(tool_name)
    budget = budget or default_budget
    _h = _um.extract_handle(tool_name, args)
    handle = _um.handle_value(_h) if _h else None  # 展示串(None 语义保留)
    text = text or ""
    low = text[:200].lower()
    is_error = any(m in low for m in _ERROR_MARKERS)
    if is_error:
        capped = budget * 2
        if len(text) <= capped:
            return text, handle, "error_full"
        return text[:capped] + "\n[truncated-error]", handle, "error_capped"
    if len(text) <= max(budget, MIN_RESULT_KEEP_CHARS):
        return text, handle, "verbatim"
    half = budget // 2
    head = text[:half].rstrip()
    tail = text[-half:].lstrip()
    marker = "[ctx-engine: %s result compressed %d→%d chars, handle: %s]" % (
        kind, len(text), len(head) + len(tail), handle or "-")
    return head + "\n" + marker + "\n" + tail, handle, kind


# 重建幂等守卫（2026-08-21 轮轮冷诊断）：compress_observation 的压缩输出恒 >
# budget（标记行自带长度），对已压缩文本再压缩会切穿旧标记继续漂移；error 截断
# 同理（capped 尾部再次被切）。Qwen3.8 GDN(SSM) 层条目 non_trimmable，后端只
# 接受完整前缀复用——absorb mismatch 重建后的转发字节必须逐字节稳定，故已含
# 引擎标记的 tool_result 一律跳过再压缩。详见 docs/background-analysis.md 轮轮冷节。
_ENGINE_MARKERS = ("[ctx-engine:", "[truncated-error]")


def _already_compressed(text):
    return any(m in text for m in _ENGINE_MARKERS)


# _msg_hash 已统一至 unit_model.msg_hash(两模块原实现同口径,单一实现化)


def _frozen_copy(msg):
    """冻结拷贝: msg/content 列表/block dict 各一层浅拷贝——后续 stage 对
    ctx.messages 的就地改写不回污染 canonical（append-only 不变量）。"""
    out = dict(msg)
    content = msg.get("content")
    if isinstance(content, list):
        out["content"] = [dict(b) if isinstance(b, dict) else b for b in content]
    return out


def _transform_message(msg, hints, session_key=None):
    """写入期压缩一条客户端消息（只动 tool_result 内容；不修改入参、不泄漏内部键）。

    session_key 非空时, 压缩丢弃的原文按 PDC 协议寄存 orig/<sid>.jsonl +
    manifest 索引行（anchor=r:<tool_use_id>）——压缩从有损变可恢复, ctx_recall
    锚点直查可取回（与 truncation.py 压缩寄存协议字段一致, 2026-09-03 补齐:
    engine 写入期压缩曾是无寄存的漏网路径）。fail-open: 寄存失败不影响请求。
    """
    content = msg.get("content")
    if not isinstance(content, list):
        return msg
    new_blocks = []
    changed = False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            hint = hints.get(id(block)) or ("", {})
            text = _result_text(block)
            # 幂等守卫（见 _ENGINE_MARKERS 注释）：已压缩/已截断结果原样保留
            if _already_compressed(text):
                new_blocks.append(block)
                continue
            compressed, handle, kind = compress_observation(hint[0], hint[1], text)
            if compressed is not text:
                # PDC 寄存: 压缩即丢弃——原文寄存 orig, 索引行落 manifest,
                # 标记里的句柄从此可兑现（与 truncation 压缩同协议）。
                if session_key and block.get("tool_use_id"):
                    try:
                        import memory_stores
                        rec_key = "r:" + str(block.get("tool_use_id"))
                        memory_stores.record_orig_content(
                            session_key, rec_key, text)
                        turn = _ps._SESSION_REQUEST_COUNT.get(session_key, 0) or 0
                        _ents = [e for e in _um.extract_key_entities(text)
                                 if e not in compressed]
                        memory_stores.MANIFEST.record_units(
                            session_key, turn, "compressed",
                            [{"anchor": rec_key, "kind": "tool_result",
                              "role": "user", "tool": str(hint[0] or ""),
                              "handle": None,
                              "size_chars": len(text),
                              "head": compressed[:240],
                              "triggers": " ".join(_ents)[:240]}])
                    except Exception:
                        pass  # 寄存失败 → 无 key 标记（原文即逝, 诚实声明）
                nb = dict(block)
                if isinstance(nb.get("content"), list):
                    nb["content"] = [{"type": "text", "text": compressed}]
                else:
                    nb["content"] = compressed
                new_blocks.append(nb)
                changed = True
                continue
        new_blocks.append(block)
    if not changed:
        return msg
    out = dict(msg)
    out["content"] = new_blocks
    return out


def _pair_tool_hints(messages):
    """把 tool_use 的 name/args 附加到对应 tool_result（压缩句柄用，不落盘）。"""
    pending = {}
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if role == "assistant":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    pending[block.get("id")] = (block.get("name", ""),
                                                block.get("input") or {})
        elif role == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id")
                    if tid in pending:
                        yield block, pending[tid]


def _rounds(messages):
    """消息列表 → 轮次分组（user 起点; system 单独）。用于 K 窗口与收编。"""
    rounds = []
    current = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            if current:
                rounds.append(current)
                current = []
            rounds.append([msg])
            continue
        if role == "user" and current:
            rounds.append(current)
            current = []
        current.append(msg)
    if current:
        rounds.append(current)
    return rounds


def _is_system_round(rnd):
    return bool(rnd) and rnd[0].get("role") == "system"


def _round_summary(rnd, turn):
    """折叠轮次 → 台账格式动作行（§4.9: 动作行 + 摘要 + 句柄，确定性无 LLM）。"""
    tools = []
    for msg in rnd:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                handle = _um.handle_value(_um.extract_handle(block.get("name", ""), args))
                target = handle or next(
                    (str(args[k])[:60] for k in _um.HANDLE_KEYS
                     if isinstance(args.get(k), str) and args[k].strip()), "")
                tools.append("- %s %s" % (block.get("name", ""), target[:120]))
    head_text = ""
    for msg in rnd:
        if msg.get("role") == "user":
            content = msg.get("content")
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"] \
                if isinstance(content, list) else [content if isinstance(content, str) else ""]
            t = " ".join(x for x in texts if isinstance(x, str)).strip()
            if t:
                head_text = t[:160]
                break
    lines = ["turn %s: %s" % (turn, head_text or "(no user text)")] + tools
    return "\n".join(lines)


class CanonicalSession(object):
    """单会话 canonical 状态: 发送视图 + 已发送指纹集合。

    M2.1(2026-08-22) 语义升级:
    - canonical = 发送视图(append-only 冻结列表)——缓存命中的是「代理发了
      什么」; 客户端历史如何扰动(compaction/改写)都不触发发送视图重建
    - sent_set = 已发送客户端消息的 hash 集合——新观测判定 = 客户端历史中
      hash 未见过者, 按客户端顺序追加; 已见过的(含被客户端 compaction
      掉后又重发的)一律跳过 → 发送前缀逐位稳定 → 缓存逐位命中
    - mismatch = 诊断信号(客户端尾部不再含最近发送的末尾消息 = append-only
      纪律被客户端破坏); 只上报不重建
    """

    def __init__(self, session_key):
        self.session_key = session_key
        self.canonical = []           # 发送视图(冻结消息, append-only)
        self.sent_set = set()         # 已发送客户端消息 hash(新观测判定)
        self.sent_order = []          # 发送视图对应的客户端指纹序(尾部检查/折叠同步)
        # user_msgs = canonical 累计 user 消息条数（引擎内部口径, 与请求轮错开:
        # tool_result 亦为 user role → 每请求轮可 +2）。EPOCH 日志用它, 台账/
        # 诊断的 turn = 客户端请求序号——两者语义不同, 排查时勿混用。
        self.user_msgs = 0
        self.epoch_count = 0
        self.last_epoch_user_msgs = 0
        self.compression_region = []  # L3 压缩区文本行（epoch 收编产物）
        self.last_sent_tokens = 0     # 后端 usage 回填(验收门禁 1 计量)
        self.last_cached_tokens = 0
        # DEF-308 轨道①: 已应答 tool_use_id 账本（增量维护）——客户端改写
        # 判别的依据（视图字节稳定化, PROXY_CTX_VIEW_STABLE_ENABLED）
        self.answered_tids = set()
        self.first_seen = time.time()
        self.last_seen = time.time()

    # ------------------------------------------------------------------ %
    def record_usage(self, prompt_tokens, cached_tokens):
        """后端 usage 回填: cached_tokens/prompt_tokens 是 Phase 1 验收门禁 1
        (增量 prefill >90%)的主口径。返回 hit_ratio(0-1; 无数据时 None)。"""
        self.last_sent_tokens = int(prompt_tokens or 0)
        self.last_cached_tokens = int(cached_tokens or 0)
        if not self.last_sent_tokens:
            return None
        return self.last_cached_tokens / self.last_sent_tokens

    # ------------------------------------------------------------------ %
    def absorb(self, client_messages):
        """吸收客户端全量历史 → (发送视图, mismatch, new_msgs)。

        M2.1(2026-08-22): 新观测判定 = sent_set(hash 集合)去重, 发送视图
        纯 append-only。缓存命中的是**代理发送了什么**, 不是**客户端说了
        什么**(§4.7 缓存纪律的正确落点; 8-22 真实任务 70% 失配→全量重建→
        TTFT 131s 的根因修复):
        - 遍历客户端新历史: hash 未见过(不在 sent_set)的消息 = 新观测, 按
          客户端顺序追加进发送视图并登记; 已见过的(含客户端 compaction 后
          重发的旧轮)一律跳过 → 发送前缀逐位稳定 → 缓存逐位命中, 与客户端
          如何改写(中间/头部/尾部)完全无关
        - 客户端 compaction 摘要是"新消息" → 自然追加; 被删除的旧段保留在
          发送视图(token 略涨, 由 epoch 收编)——稳定的前缀优先于紧凑性
        - mismatch = 诊断信号: 客户端尾部不再含最近发送的末尾消息
          (append-only 纪律被客户端破坏, §4.10 边界 2); 只上报不重建
          (发送视图不变, 缓存不受影响; 重建反而击穿)
        """
        now = time.time()
        self.last_seen = now
        fp = [_um.msg_hash(m) for m in (client_messages or [])]
        fp_set = set(fp)
        # 失配信号: 最近发送的末尾消息不在客户端本次历史 → 尾部被改
        tail_hash = self.sent_order[-1] if self.sent_order else None
        mismatch = bool(tail_hash and tail_hash not in fp_set)
        # 新观测 = hash 未见过(按客户端顺序收集, 再统一压缩冻结)
        stable = getattr(_ps, "PROXY_CTX_VIEW_STABLE_ENABLED", False)
        new_items = []
        skipped_rewrites = 0
        for msg in (client_messages or []):
            h = _um.msg_hash(msg)
            if h in self.sent_set:
                continue
            if stable:
                # DEF-308 轨道①: 客户端改写判别——tool_result 全部指向已
                # 应答 tid 的消息是改写副本(墓碑化), canonical 已持有该
                # exchange 的已发送完整版; 追加副本会制造重复 tool_result,
                # 触发配对修复回写已发送前缀 → 后端整条匹配 MISS → 全额
                # 冷 prefill。跳过/剥离后视图只增不缩, 字节逐位稳定。
                verdict, stripped = self._classify_rewrite(msg)
                if verdict == "skip":
                    skipped_rewrites += 1
                    continue
                if verdict == "strip":
                    msg = stripped
                    h = _um.msg_hash(msg)
                    if h in self.sent_set:
                        continue
            self.sent_set.add(h)
            self.sent_order.append(h)
            new_items.append(msg)
        hints = {id(b): hint for b, hint in _pair_tool_hints(client_messages or [])}
        for msg in new_items:
            self.canonical.append(_frozen_copy(
                _transform_message(msg, hints, self.session_key)))
            self._note_answered(msg)
        new_user_msgs = sum(1 for m in new_items if m.get("role") == "user")
        if new_user_msgs:
            self.user_msgs += max(1, new_user_msgs)
        if skipped_rewrites:
            try:
                from proxy_logging import log as _log
                _log("  -> [context_engine] view-stable: skipped %d client "
                     "rewrite copy(s), prefix bytes unchanged"
                     % skipped_rewrites)
            except Exception:
                pass
        return self.canonical, mismatch, len(new_items)

    def _classify_rewrite(self, msg):
        """DEF-308 轨道①: 客户端改写判别 → ("append"|"skip"|"strip", 副本)。

        user 消息的 tool_result 块**全部**指向 canonical 已应答 tid（该
        exchange 此前轮已发送）→ 本条是客户端改写副本（SDK 墓碑化等）：
          - 无其他内容块 → ("skip", None)：canonical 保留已发送完整版
          - 混有文本/新块 → ("strip", 剥离已应答块后的副本)：新内容照常追加
        含未应答 tid / 非 user / 非 tool_result 消息 → ("append", None)。
        """
        if msg.get("role") != "user":
            return "append", None
        blocks = msg.get("content")
        if not isinstance(blocks, list):
            return "append", None
        tr = [b for b in blocks
              if isinstance(b, dict) and b.get("type") == "tool_result"]
        if not tr:
            return "append", None
        if not all(str(b.get("tool_use_id") or "") in self.answered_tids
                   for b in tr):
            return "append", None  # 含新结果 → 正常追加
        rest = [b for b in blocks
                if not (isinstance(b, dict) and b.get("type") == "tool_result")]
        if not rest:
            return "skip", None
        stripped = dict(msg)
        stripped["content"] = rest
        return "strip", stripped

    def _note_answered(self, msg):
        """追加消息后登记其 tool_use_id 到已应答账本（增量, 无界但量级小）。"""
        blocks = msg.get("content")
        if not isinstance(blocks, list):
            return
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_result" \
                    and b.get("tool_use_id"):
                self.answered_tids.add(str(b["tool_use_id"]))

    # ------------------------------------------------------------------ %
    def est_tokens(self, messages=None):
        """canonical（或指定列表）的 token 估算（序列化长度 /4 口径）。

        压缩区不单独加计: 只要压缩区非空, 台账消息就内嵌在 canonical /
        折叠视图里（_collapse 组装时写入）, 从目标消息本身序列化已覆盖。
        单独加计会把 epoch 后的视图重复计一遍（over-estimate → 提前误触
        下一轮 epoch）。
        """
        import json as _json
        target = self.canonical if messages is None else messages
        try:
            chars = len(_json.dumps(target, ensure_ascii=False,
                                    default=str))
        except (TypeError, ValueError):
            chars = sum(len(repr(m)) for m in target)
        return estimate_tokens(chars)

    def _real_scale(self, messages=None):
        """真实 prompt_tokens 口径: 后端 usage 回填优先(上一轮实测值);
        无回填(会话首轮/回填缺失)时用 est × EST_REAL_RATIO 换算。
        messages 参数用于收编产物容量判定(产物未发送、无 usage)。"""
        if messages is None and self.last_sent_tokens > 0:
            return self.last_sent_tokens
        return int(self.est_tokens(messages) * EST_REAL_RATIO)

    def _trigger_scale(self):
        """#50(2026-08-29): 触发判定专用口径——上轮实测回填(last_sent_tokens)
        与本轮 est×R 取大。原判定只看 _real_scale() 的回填分支, 而回填是
        **上一轮**的实测: 本轮新增内容(长工具结果等)不在内, 大增量轮会在
        「上轮 60K 未超 → 本轮真实 71K」的窗口里过冲(2026-08-22 42355d18
        实测: est 51.2K / 真实 82K, swap 挤兑)。est×R 覆盖本轮全量, 两者
        取大 = 回填精度与增量覆盖兼得。"""
        candidates = []
        if self.last_sent_tokens > 0:
            candidates.append(self.last_sent_tokens)
        candidates.append(int(self.est_tokens() * EST_REAL_RATIO))
        return max(candidates)

    def maybe_epoch(self, trigger_tokens, window_k):
        """发送前 epoch 检查 → (epoch_triggered, final_messages)。

        触发与容量判定均为**真实 prompt_tokens 口径**(2026-08-22 校准, 见
        EST_REAL_RATIO 注释): est(/4) 低估 1.6×, est 口径下 S=60K 等于真实
        ~96K——epoch 永远在系统崩溃(实测 82K)之后才到, 形同虚设。
        触发用 _trigger_scale(#50: 上轮回填与本轮 est×R 取大, 防大增量轮
        过冲); 压缩后容量复检仍用 _real_scale(messages)。
        未超限: canonical 原样（append-only, 前缀 = 上轮所发）。
        超限: 一次 epoch——K 窗口外轮次收编入压缩区, 重切为 L0 + 压缩区
              + 最近 K 轮并**回写 canonical**（后续轮在其上继续 append）;
              压缩后仍超限 → K=4 收紧 → L3 减半 → 仍超限返回 None
              （硬上限: 调用方返回 context 超限错误, §4.9 回退保护）。
        """
        if self._trigger_scale() <= trigger_tokens:
            return False, list(self.canonical)
        self.epoch_count += 1
        self.last_epoch_user_msgs = self.user_msgs
        # 每次回退尝试都从同一基线重算压缩区——_collapse 会就地追加
        # self.compression_region, 不重置会把 K=24 失败尝试的台账行重复
        # 累计进 K=4 的产物(重复行膨胀 → 误触硬上限)。
        base_region = list(self.compression_region)
        # L-9/DEF-313(2026-09-07): 保留口径 token 化——触发按 S(token) 而
        # 保留按 K(轮数) 的错配, 导致折叠时机与保留体量脱节(EXP-3 v2 实测
        # 双/三折叠再膨胀)。PROXY_CTX_KEEP_TOKEN_BUDGET>0 时, 保留窗口改按
        # token 预算自最新轮回保(回退阶梯: 全额→半额→旧 K=4+减半)。
        keep_budget = int(getattr(_ps, "PROXY_CTX_KEEP_TOKEN_BUDGET", 0) or 0)
        if keep_budget > 0:
            ladder = ((keep_budget, False), (max(2, keep_budget // 2), False),
                      (4, True))
        else:
            ladder = ((window_k, False), (4, False), (4, True))
        for k, halve in ladder:
            self.compression_region = list(base_region)
            messages = self._collapse(k, halve,
                                      keep_budget=(k if keep_budget > 0 else 0))
            if self._real_scale(messages) <= trigger_tokens:
                self.canonical = messages
                self._sync_order_after_collapse(messages)
                return True, messages
        return True, None

    def _sync_order_after_collapse(self, messages):
        """epoch 回写后同步 sent_order（折叠视图 = system + 压缩区 + K 窗口）。

        sent_order 取旧序列尾部 keep 条(即 K 窗口对应指纹)；
        sent_set **不收缩**——折叠掉的旧消息 hash 仍登记, 客户端重发旧轮时
        会被跳过, 不会错误地重新 append(发送视图稳定性不因 epoch 破坏)。
        """
        keep_msgs = sum(1 for m in messages
                        if m.get("role") != "system" and not m.get("_ctx_engine_epoch"))
        if keep_msgs <= 0:
            self.sent_order = []
            return
        tail = self.sent_order[-keep_msgs:]
        self.sent_order = list(tail)

    def _collapse(self, window_k, halve, keep_budget=0):
        """K 窗口外轮次 → 压缩区动作行; 返回重切后的消息列表（不落 self.canonical）。

        L-9/DEF-313: keep_budget>0 时保留窗口按 token 预算自最新轮回保
        （至少最近 2 轮），window_k 仅作预算=0 时的旧口径。"""
        rounds = _rounds(self.canonical)
        system_rounds = [r for r in rounds if _is_system_round(r)]
        body_rounds = [r for r in rounds if not _is_system_round(r)]
        if keep_budget > 0:
            keep, acc = [], 0
            for rnd in reversed(body_rounds):
                cost = self._real_scale(list(rnd))
                if keep and acc + cost > keep_budget:
                    break
                keep.insert(0, rnd)
                acc += cost
            if len(keep) < 2 and len(body_rounds) >= 2:
                keep = body_rounds[-2:]  # 兜底: 至少保留最近 2 轮
            kcount = len(keep)
            collect = body_rounds[:-kcount] if kcount else list(body_rounds)
        else:
            keep = body_rounds[-window_k:] if window_k else []
            collect = body_rounds[:-window_k] if window_k else body_rounds
        # R10.1 manifest: 收编轮次留索引行(页表;折叠面板行已含摘要,索引行
        # 提供可寻址性;fail-open)
        if getattr(_ps, "PROXY_PD_ENABLED", True) and collect and self.session_key:
            try:
                import memory_stores
                memory_stores.record_dropped_messages(
                    self.session_key, self.user_msgs, "epoch_collapse",
                    [m for rnd in collect for m in rnd])
            except Exception:
                pass
            # L-11/DEF-307(2026-09-06): epoch 折叠原文寄存——此前只写索引行,
            # recover_full_content 的 (anchor, turn) 按 archive 精确轮号必
            # miss(manifest 行带的是折叠时刻轮号, 内容躺在早期轮), epoch 域
            # 召回数据面断裂。与写入期压缩同协议寄存 orig/(同 anchor 键,
            # 读取取最后一条)。只寄存 r: 单元(u: 无正文)且超零头阈值;
            # fail-open 不影响折叠本体。
            try:
                import memory_stores as _ms
                _seen = set()
                for rnd in collect:
                    for m in rnd:
                        if m.get("role") != "user":
                            continue
                        _blocks = m.get("content")
                        if not isinstance(_blocks, list):
                            continue
                        for _b in _blocks:
                            if not isinstance(_b, dict) \
                                    or _b.get("type") != "tool_result" \
                                    or not _b.get("tool_use_id"):
                                continue
                            _key = "r:" + str(_b["tool_use_id"])
                            if _key in _seen:
                                continue
                            _text = _um.result_text(_b, None)
                            if len(_text) < 200:
                                continue
                            _seen.add(_key)
                            _ms.record_orig_content(self.session_key, _key,
                                                    _text)
            except Exception:
                pass
        new_lines = []
        for i, rnd in enumerate(collect):
            new_lines.append(_round_summary(rnd, i + 1))
        region = self.compression_region + new_lines
        if halve and len(region) > 20:
            # 留尾半段: 最新收编轮次紧邻保留窗口, 是模型「最近在做什么」的最直接
            # 记忆(§4.4 近期窗口是元认知主作用区)——留头丢尾会记远古忘刚才。
            region = region[-max(10, len(region) // 2):]
        self.compression_region = region
        # 折叠召回线索（2026-09-03 folded-recall-cue 设计）：与 A 路同 cue
        # （ctx_recall.RECALL_CUE 唯一源头）；键取被收编轮次 anchor（复用
        # truncation pin 跳过的 unit_anchors 写法）。fail-open：失败则无 cue
        # 后缀，面板主体不受影响。
        try:
            from ctx_recall import RECALL_CUE as _RECALL_CUE
            from ctx_recall import recall_keys_line as _recall_keys_line
            import ifc_metrics as _ifm
            _fold_anchors = []
            for rnd in collect:
                for m in rnd:
                    try:
                        _units = _ifm.unit_anchors(m)
                    except Exception:
                        _units = []
                    for u in _units:
                        a = u.get("anchor", "") if isinstance(u, dict) else ""
                        if (isinstance(a, str) and a[:2] in ("r:", "u:")
                                and a not in _fold_anchors):
                            _fold_anchors.append(a)
            _cue_suffix = "\n" + _RECALL_CUE
            _keys_suffix = _recall_keys_line(_fold_anchors)
            if _keys_suffix:
                _cue_suffix += "\n" + _keys_suffix
        except Exception:
            _cue_suffix = ""
        out = []
        for r in system_rounds:
            out.extend(r)
        # DEF-311(2026-09-07): 政策文本钉住——被收编轮次里的
        # <system-reminder>/<test_env> 段落是"指令面"(环境契约卡/CLAUDE.md
        # 等), 折叠台账化会把指引永久挤出视图(EXP-3 v2 实测 5/6 深卡臂
        # 会话最新轮失卡)。折叠时从被收编 user 消息提取此类段落, 去重后
        # 钉在台账头部(总预算 8000 字符, 超出截断并注明); 台账被后续折叠
        # 收编时本提取重复执行, 段落随去重跨折叠延续。fail-open: 提取失败
        # 不影响折叠本体。
        _pinned = ""
        try:
            import json as _json
            import hashlib as _hashlib
            _segs, _seen_seg = [], set()
            _pat = re.compile(
                r"(<system-reminder>.*?</system-reminder>"
                r"|<test_env>.*?</test_env>)",
                re.DOTALL)
            for rnd in collect:
                for m in rnd:
                    if m.get("role") != "user":
                        continue
                    _c = m.get("content")
                    _text = _c if isinstance(_c, str) else _json.dumps(
                        _c, ensure_ascii=False)
                    for _seg in _pat.findall(_text):
                        _k = _hashlib.sha256(_seg.encode("utf-8")).hexdigest()
                        if _k in _seen_seg:
                            continue
                        _seen_seg.add(_k)
                        _segs.append(_seg)
            if _segs:
                _pin_budget = 8000
                _parts, _used = [], 0
                for _seg in _segs:
                    if _used >= _pin_budget:
                        _parts.append("[policy pinned: further segments truncated]")
                        break
                    _take = _seg[:max(0, _pin_budget - _used)]
                    _parts.append(_take)
                    _used += len(_take)
                _pinned = ("[policy pinned from collapsed rounds]\n"
                           + "\n".join(_parts) + "\n")
            # DEF-313 观测面: 曾见政策段(_policy_seen)而本次折叠未提取到
            # → 说明政策在被收编面里丢失, 打 WARN 一次(防"静默失卡"类
            # 缺陷再溜进生产——EXP-3 v2 的失卡即人工取证才发现)。
            if _segs:
                self._policy_seen = True
            elif getattr(self, "_policy_seen", False) and not getattr(
                    self, "_policy_warned", False):
                self._policy_warned = True
                from proxy_logging import log as _plog
                _plog("  -> [context_engine] WARN: policy segment expected "
                      "but absent in collapsed rounds — possible silent loss "
                      "(DEF-313 observability)")
        except Exception:
            _pinned = ""
        if region:
            out.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": "[context-engine epoch %d: %d earlier rounds collapsed "
                            "to action ledger below; handles preserved]\n%s%s%s" % (
                                self.epoch_count, len(collect), _pinned,
                                "\n".join(region), _cue_suffix),
                }],
                "_ctx_engine_epoch": True,
            })
        for r in keep:
            out.extend(r)
        return out


class EngineStore(object):
    """会话注册表: TTL/FIFO 有界（复用 ledger 治理模式）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}  # key → CanonicalSession（插入序, FIFO 用）
        self._epoch_last_turn = {}  # key → epoch 触发的 turn（诊断 is_epoch_turn）

    def get_or_create(self, key):
        with self._lock:
            self._evict_locked()
            sess = self._sessions.get(key)
            if sess is None:
                sess = CanonicalSession(key)
                self._sessions[key] = sess
            return sess

    def mark_epoch_turn(self, key, turn):
        with self._lock:
            self._epoch_last_turn[key] = turn

    def is_epoch_turn(self, key, turn):
        with self._lock:
            return self._epoch_last_turn.get(key) == turn

    def epoch_count(self, key):
        with self._lock:
            sess = self._sessions.get(key)
            return sess.epoch_count if sess else 0

    def _evict_locked(self):
        ttl = max(300, getattr(_ps, "PROXY_DIAG_SESSION_TTL_MIN", 180)) * 60
        now = time.time()
        stale = [k for k, s in self._sessions.items() if now - s.last_seen > ttl]
        for k in stale:
            del self._sessions[k]
            self._epoch_last_turn.pop(k, None)
        max_sessions = max(4, getattr(_ps, "PROXY_DIAG_SESSION_MAX", 64))
        while len(self._sessions) > max_sessions:
            k = next(iter(self._sessions))
            del self._sessions[k]
            self._epoch_last_turn.pop(k, None)


ENGINE = EngineStore()


def effective_trigger_tokens():
    """S: 0/auto → min(65% × ctx_chars/4, DEFAULT)（reloadable 热读）。
    口径: 真实 prompt_tokens(2026-08-22 校准)——显式配置值直接采用,
    auto 推导的 65%×ctx/4 恰为同量级真实触发点, 无需再乘系数。"""
    raw = getattr(_ps, "PROXY_CTX_EPOCH_TRIGGER_TOKENS", 0) or 0
    if raw and raw > 0:
        return int(raw)
    ctx_chars = getattr(_ps, "PROXY_CTX_CHARS_LIMIT", 400000) or 400000
    return min(int(ctx_chars * 0.65 // TOKEN_CHAR_RATIO), DEFAULT_EPOCH_TRIGGER_TOKENS)


def effective_window_k():
    raw = getattr(_ps, "PROXY_CTX_WINDOW_K", 0) or 0
    return int(raw) if raw > 0 else DEFAULT_WINDOW_K


__all__ = [
    "ENGINE", "CanonicalSession", "EngineStore", "ContextOverflowError",
    "compress_observation", "estimate_tokens",
    "effective_trigger_tokens", "effective_window_k",
    "DEFAULT_EPOCH_TRIGGER_TOKENS", "DEFAULT_WINDOW_K", "TOKEN_CHAR_RATIO",
]
