"""Content compressor: TokenSieve-inspired semantic compression for tool results.
"""
import json
import math
import re

import proxy_state

# Phase 2: TokenSieve-inspired content compression for tool_result payloads.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# TS-1: BM25 scoring for relevance-driven compression decisions (W3 d1-d2)
# ---------------------------------------------------------------------------

_BM25_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]")

# Module-level cross-request IDF state (process-wide, survives reload).
_BM25_IDF_MAP = {}
_BM25_IDF_DOC_FREQ = {}
_BM25_IDF_TOTAL_DOCS = 0


def _text_str(value):
    """Defensive text normalization.

    2026-08-17 实测: 部分消息 content 块的 "text" 字段是 list 形态
    (上游/转换器的特定消息形态),直接进 str.join 会崩
    (TypeError: sequence item 0: expected str instance, list found),
    代理 500 导致本地臂批跑整任务死亡(swe38-sy 会话实测)。

    实现已统一至 unit_model.text_str(词汇表对齐,2026-08-29);保留薄委托。
    """
    import unit_model as _um
    return _um.text_str(value)


def _bm25_tokenize(text, min_prefix=4):
    """Tokenize text for BM25 scoring.

    English: identifier-like tokens (letters, digits, underscores).
    Chinese: single-character segmentation via CJK Unified Ideographs range.
    Lowercase normalization.
    Prefix expansion: for each English token >= min_prefix chars, also emit
    its first min_prefix characters as a variant matching key.
    """
    text = _text_str(text)  # 防 list 型 text 块(2026-08-17 实测)
    tokens = []
    for m in _BM25_TOKEN_RE.finditer(text):
        token = m.group(0)
        if '\u4e00' <= token[0] <= '\u9fff':
            tokens.append(token)
        else:
            lower = token.lower()
            tokens.append(lower)
            if len(lower) >= min_prefix:
                tokens.append(lower[:min_prefix])
    return tokens


def _bm25_idf(token, min_prefix=4):
    """Return the IDF for a token.

    Uses the module-level _BM25_IDF_MAP. For unknown tokens, tries prefix
    expansion: finds the closest known token sharing the first min_prefix
    characters and returns its IDF. If no prefix match exists, returns the
    default IDF (log(1 + N/1) ≈ log(1 + total_docs)). When total_docs is 0
    (IDF not yet initialized), returns 1.0 as a neutral default.
    """
    if token in _BM25_IDF_MAP:
        return _BM25_IDF_MAP[token]
    if len(token) >= min_prefix:
        prefix = token[:min_prefix]
        for known in _BM25_IDF_MAP:
            if known.startswith(prefix):
                return _BM25_IDF_MAP[known]
    n = _BM25_IDF_TOTAL_DOCS
    if n > 0:
        return math.log(1.0 + n / 1.0)
    return 1.0  # neutral default when IDF not yet initialized


def _update_idf(messages, min_prefix=4):
    """Incrementally update IDF from a batch of messages.

    Each message is treated as a document. Token frequencies are accumulated
    into _BM25_IDF_DOC_FREQ (number of documents containing each token).
    After update, _BM25_IDF_MAP is recomputed as log(1 + (N - df + 0.5) / (df + 0.5)).

    LRU: if _BM25_IDF_MAP exceeds 10000 entries, prune the lowest-frequency
    entries (by doc_freq) until under 8000.
    """
    global _BM25_IDF_TOTAL_DOCS
    doc_tokens = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                doc_tokens.update(_bm25_tokenize(block.get("text", ""), min_prefix))
            elif block.get("type") == "tool_result":
                tc = block.get("content", "")
                if isinstance(tc, list):
                    for sub in tc:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            doc_tokens.update(_bm25_tokenize(sub.get("text", ""), min_prefix))
                elif isinstance(tc, str):
                    doc_tokens.update(_bm25_tokenize(tc, min_prefix))

    if not doc_tokens:
        return

    _BM25_IDF_TOTAL_DOCS += 1
    for token in doc_tokens:
        _BM25_IDF_DOC_FREQ[token] = _BM25_IDF_DOC_FREQ.get(token, 0) + 1

    n = _BM25_IDF_TOTAL_DOCS
    for token, df in _BM25_IDF_DOC_FREQ.items():
        _BM25_IDF_MAP[token] = math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    # LRU prune: keep only the top 8000 by doc_freq when exceeding 10000.
    if len(_BM25_IDF_MAP) > 10000:
        sorted_tokens = sorted(_BM25_IDF_MAP.keys(), key=lambda t: _BM25_IDF_DOC_FREQ.get(t, 0))
        for token in sorted_tokens[:2000]:
            _BM25_IDF_MAP.pop(token, None)
            _BM25_IDF_DOC_FREQ.pop(token, None)


def bm25_score_message(msg, query, idf_map=None, k1=1.5, b=0.75, min_prefix=4):
    """Compute Okapi BM25 score for a single message relative to a query.

    Args:
        msg: Anthropic-format message dict (role + content list/text).
        query: User intent text (typically the last user message).
        idf_map: Token → IDF dict; None uses module-level _BM25_IDF_MAP.
        k1, b: Okapi BM25 parameters (default 1.5/0.75, matching litellm).
        min_prefix: Minimum prefix length for expansion (default 4).

    Returns:
        float BM25 score. Higher = more relevant. 0.0 for empty msg/query.
    """
    if not isinstance(msg, dict) or not query:
        return 0.0

    query_tokens = _bm25_tokenize(query, min_prefix)
    if not query_tokens:
        return 0.0

    content = msg.get("content")
    if not isinstance(content, list):
        return 0.0

    # Extract text from the message content blocks.
    doc_text_parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                doc_text_parts.append(_text_str(block.get("text", "")))
            elif block.get("type") == "tool_result":
                tc = block.get("content", "")
                if isinstance(tc, list):
                    for sub in tc:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            doc_text_parts.append(_text_str(sub.get("text", "")))
                elif isinstance(tc, str):
                    doc_text_parts.append(tc)
    doc_text = " ".join(doc_text_parts)
    doc_tokens = _bm25_tokenize(doc_text, min_prefix)
    if not doc_tokens:
        return 0.0

    # Count term frequencies in the document.
    tf_map = {}
    for t in doc_tokens:
        tf_map[t] = tf_map.get(t, 0) + 1

    doc_len = len(doc_tokens)
    avg_doc_len = _BM25_IDF_TOTAL_DOCS if _BM25_IDF_TOTAL_DOCS > 0 else 100.0

    score = 0.0
    for qt in set(query_tokens):
        tf = tf_map.get(qt, 0)
        if tf == 0:
            continue
        if idf_map is not None:
            idf = idf_map.get(qt, 0.0)
        else:
            idf = _bm25_idf(qt, min_prefix)
        if idf <= 0:
            continue
        numerator = tf * (k1 + 1)
        denominator = tf + k1 * (1 - b + b * doc_len / avg_doc_len)
        score += idf * numerator / denominator

    return score


def _extract_last_user_text(messages):
    """Return the plain text of the last user message that has text blocks.

    Skips user messages that only contain tool_result blocks (no text).
    Returns empty string if no qualifying user message is found.
    """
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(_text_str(block.get("text", "")))
        if text_parts:
            return " ".join(text_parts)
    return ""

_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*m')


def _scrub_ansi(text):
    """Remove ANSI color/control escape sequences from terminal output."""
    if not isinstance(text, str):
        text = str(text)
    return _ANSI_ESCAPE_RE.sub('', text)


def _detect_content_type(text, mime_hint=None):
    """Detect whether a tool_result payload is json, code, log, or plain text."""
    if mime_hint:
        hint = mime_hint.lower()
        if "json" in hint:
            return "json"
        if any(k in hint for k in ("html", "xml", "markdown")):
            return "text"
        if any(k in hint for k in ("python", "javascript", "typescript", "rust", "go", "c++", "c", "java")):
            return "code"
    if not isinstance(text, str):
        return "text"
    stripped = text.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or \
       (stripped.startswith("[") and stripped.endswith("]")):
        try:
            json.loads(stripped)
            return "json"
        except Exception:
            pass
    # Log heuristic: lines starting with timestamps or level keywords.
    lines = stripped.splitlines()
    if len(lines) >= 2:
        log_markers = 0
        for line in lines[:10]:
            if re.search(r"^\d{4}[-/]\d{2}[-/]\d{2}|^\d{2}:\d{2}:\d{2}|\b(ERROR|WARN|INFO|DEBUG|FATAL)\b", line):
                log_markers += 1
        if log_markers >= 1:
            return "log"
    # Code heuristic: significant syntax markers or indentation pattern.
    code_markers = sum(1 for kw in ("def ", "class ", "import ", "function ", "const ", "let ", "var ", "#include", "return ")
                       if kw in text)
    indented_lines = sum(1 for line in lines if line.startswith(("    ", "\t")))
    if code_markers >= 1 or (len(lines) >= 3 and indented_lines >= 1):
        return "code"
    return "text"


def _sieve_json(obj, max_items=None, max_str_len=None, max_depth=None,
                seen_strings=None, enable_dedupe=False, _depth=0):
    """Summarize JSON while preserving structure.

    - Arrays keep first max_items entries plus a count note.
    - Strings longer than max_str_len are truncated.
    - Nesting deeper than max_depth is stringified.
    - Optional scalar dedupe (first-seen-wins) for long repeated strings.
    """
    if max_items is None:
        max_items = proxy_state.PROXY_SIEVE_JSON_MAX_ITEMS
    if max_str_len is None:
        max_str_len = proxy_state.PROXY_SIEVE_JSON_MAX_STR_LEN
    if max_depth is None:
        max_depth = proxy_state.PROXY_SIEVE_JSON_MAX_DEPTH
    if seen_strings is None:
        seen_strings = {}

    if _depth > max_depth:
        return str(obj)[:max_str_len]

    if isinstance(obj, str):
        if enable_dedupe and len(obj) > 20:
            if obj in seen_strings:
                return f"###(repeated: {seen_strings[obj]})"
            seen_strings[obj] = len(seen_strings) + 1
        if len(obj) > max_str_len:
            return obj[:max_str_len] + f"...[truncated {len(obj) - max_str_len} chars]"
        return obj

    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return obj
    if obj is None:
        return None

    if isinstance(obj, list):
        if len(obj) > max_items:
            summarized = [_sieve_json(item, max_items, max_str_len, max_depth,
                                      seen_strings, enable_dedupe, _depth + 1)
                          for item in obj[:max_items]]
            return summarized + [f"...({len(obj) - max_items} more items)"]
        return [_sieve_json(item, max_items, max_str_len, max_depth,
                            seen_strings, enable_dedupe, _depth + 1)
                for item in obj]

    if isinstance(obj, dict):
        return {
            k: _sieve_json(v, max_items, max_str_len, max_depth,
                           seen_strings, enable_dedupe, _depth + 1)
            for k, v in obj.items()
        }

    return str(obj)[:max_str_len]


def _compress_code(text):
    """Remove comments and collapse excessive blank lines while keeping code."""
    if not isinstance(text, str):
        text = str(text)
    lines = text.splitlines()
    result = []
    prev_blank = False
    for line in lines:
        stripped = line.strip()
        # Drop full-line comments (common languages)
        if stripped.startswith(("#", "//", "/*", "*", "--", ";")):
            continue
        # Drop trailing comments
        for marker in ("//", "#", "--"):
            idx = line.find(marker)
            if idx >= 0:
                line = line[:idx]
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        result.append(line.rstrip())
        prev_blank = is_blank
    return "\n".join(result).strip()


def _compress_log(text, dedupe=True):
    """Deduplicate adjacent log lines and strip common timestamps.
    Keep lines containing error/exception/warning keywords."""
    if not isinstance(text, str):
        text = str(text)
    lines = text.splitlines()
    out = []
    last_line = None
    dup_count = 0
    for line in lines:
        # Strip common timestamp prefixes
        cleaned = re.sub(r"^\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?\s*", "", line)
        cleaned = re.sub(r"^\d{2}:\d{2}:\d{2}\s*", "", cleaned)
        if dedupe and cleaned == last_line:
            dup_count += 1
            continue
        if dup_count > 0:
            out.append(f"...({dup_count} identical lines omitted)")
            dup_count = 0
        last_line = cleaned
        # Prioritize error lines by keeping them verbatim; compress benign lines
        if re.search(r"\b(ERROR|Exception|Traceback|FATAL|CRITICAL)\b", cleaned, re.IGNORECASE):
            out.append(line)  # keep original with timestamp for errors
        else:
            out.append(cleaned)
    if dup_count > 0:
        out.append(f"...({dup_count} identical lines omitted)")
    return "\n".join(out).strip()


def _aggressive_truncate(text, ratio=0.3):
    """Aggressively truncate text to approximately `ratio` of original length.

    Keeps head (first 20%) and tail (last 10%) of the target length to
    preserve context boundaries, discarding the middle.
    """
    if not isinstance(text, str):
        text = str(text)
    target = max(int(len(text) * ratio), 200)
    if len(text) <= target:
        return text
    head_len = int(target * 0.7)
    tail_len = target - head_len
    return text[:head_len] + f"\n...[BM25 aggressive: truncated {len(text) - target} chars]\n" + text[-tail_len:]


def _compress_text(text, max_len=2000):
    """Truncate very long plain text while keeping first/last context."""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= max_len:
        return text
    head = text[:max_len // 2]
    tail = text[-max_len // 2:]
    return f"{head}\n\n...[truncated {len(text) - max_len} chars]\n\n{tail}"


def _dedupe_scalars(obj, min_len=20, seen=None):
    """First-seen-wins scalar deduplication for JSON/string values."""
    if seen is None:
        seen = {}
    if isinstance(obj, str):
        if len(obj) >= min_len:
            if obj in seen:
                return f"###(repeated: {seen[obj]})"
            seen[obj] = len(seen) + 1
        return obj
    if isinstance(obj, list):
        return [_dedupe_scalars(item, min_len, seen) for item in obj]
    if isinstance(obj, dict):
        return {k: _dedupe_scalars(v, min_len, seen) for k, v in obj.items()}
    return obj


def _audit_compression(original, compressed, content_type):
    """Validate that compression did not destroy syntax/semantics."""
    if not proxy_state.PROXY_COMPRESS_AUDIT:
        return True
    if content_type == "json":
        try:
            json.loads(compressed)
            return True
        except Exception:
            return False
    if content_type == "code":
        # Simple balance check: brackets and quotes should still be roughly paired.
        open_br = compressed.count("(") + compressed.count("[") + compressed.count("{")
        close_br = compressed.count(")") + compressed.count("]") + compressed.count("}")
        return abs(open_br - close_br) <= 2
    # text/log: always pass the audit
    return True


def _structured_compress(original, mime_hint=None, mode=None):
    """Type-aware structured compression: scrub → detect → route → audit.

    Extracted from compress_tool_result (TS-4) so the BM25 drop path can
    reuse the layered compressors (json_sieve / code_compress / log_compress)
    instead of blind head/tail truncation.

    Returns (compressed, content_type, strategy, audit_pass). The caller is
    responsible for the length-threshold guard; this helper always compresses.
    """
    if mode is None:
        mode = proxy_state.PROXY_COMPRESS_MODE
    scrubbed = _scrub_ansi(original) if proxy_state.PROXY_SCRUB_ANSI else original
    content_type = _detect_content_type(scrubbed, mime_hint=mime_hint)

    if content_type == "json":
        try:
            parsed = json.loads(scrubbed)
            enable_dedupe = proxy_state.PROXY_DEDUPE_SCALARS and mode == "aggressive"
            compressed_obj = _sieve_json(parsed, enable_dedupe=enable_dedupe)
            if proxy_state.PROXY_DEDUPE_SCALARS and mode == "aggressive":
                compressed_obj = _dedupe_scalars(compressed_obj)
            compressed = json.dumps(compressed_obj, ensure_ascii=False, separators=(',', ':'))
            strategy = "json_sieve"
        except Exception:
            compressed = scrubbed
            strategy = "json_passthrough"
    elif content_type == "code":
        compressed = _compress_code(scrubbed)
        strategy = "code_compress"
    elif content_type == "log":
        compressed = _compress_log(scrubbed, dedupe=proxy_state.PROXY_LOG_DEDUPE)
        strategy = "log_compress"
    else:
        compressed = _compress_text(scrubbed)
        strategy = "text_truncate"

    audit_pass = _audit_compression(scrubbed, compressed, content_type)
    if not audit_pass:
        compressed = scrubbed
        strategy = "audit_fallback"
    return compressed, content_type, strategy, audit_pass




def compress_tool_result(content, mime_hint=None, threshold=None, mode=None,
                         bm25_score=None,
                         bm25_drop_threshold=None,
                         bm25_keep_threshold=None):
    """Compress a single tool_result content payload.

    TS-1 (W3 d3): Added bm25_score/bm25_drop_threshold/bm25_keep_threshold
    kwargs for BM25 relevance-driven compression decisions.

    - bm25_score < bm25_drop_threshold → force compress to ~30% original length
    - bm25_score >= bm25_keep_threshold → skip compression (keep verbatim)
    - bm25_score is None → fall back to existing threshold + content_type path

    Returns a dict:
        {
            "original": str,
            "compressed": str,
            "content_type": str,
            "strategy": str,
            "audit_pass": bool,
            "ratio": float,
        }
    """
    if threshold is None:
        threshold = proxy_state.PROXY_COMPRESS_THRESHOLD
    if mode is None:
        mode = proxy_state.PROXY_COMPRESS_MODE
    if bm25_drop_threshold is None:
        bm25_drop_threshold = proxy_state.PROXY_BM25_DROP_THRESHOLD
    if bm25_keep_threshold is None:
        bm25_keep_threshold = proxy_state.PROXY_BM25_KEEP_THRESHOLD

    original = content if isinstance(content, str) else str(content)
    original_len = len(original)

    def _result(compressed, content_type, strategy, audit_pass, ratio):
        """Build result dict with TS-3 CompressionSubResult fields."""
        return {
            "original": original,
            "compressed": compressed,
            "content_type": content_type,
            "strategy": strategy,
            "audit_pass": audit_pass,
            "ratio": round(ratio, 4),
            "original_len": original_len,
            "compressed_len": len(compressed),
            "bm25_score": bm25_score,
        }

    # TS-1: BM25 relevance override.
    if bm25_score is not None:
        if bm25_score >= bm25_keep_threshold:
            return _result(original, "bm25_keep", "none", True, 1.0)
        if bm25_score < bm25_drop_threshold:
            # TS-4 (2026-08-18 日志分析落地): 全史 89.9% 的压缩命中此分支,
            # 旧实现直接 30% 头尾截断,json/log 分层压缩器成为死路径。现在先做
            # 类型感知的结构化压缩;结果仍高于目标比例(且原文超过 threshold)
            # 时再叠加头尾截断封顶,保证尺寸上限语义不回退。
            structured, ctype, strategy, audit_pass = _structured_compress(
                original, mime_hint=mime_hint, mode=mode)
            target_len = original_len * proxy_state.PROXY_BM25_DROP_TARGET_RATIO
            if original_len >= threshold and len(structured) > target_len:
                cap_ratio = target_len / len(structured)
                structured = _aggressive_truncate(structured, ratio=cap_ratio)
                strategy += "+cap"
            r = len(structured) / original_len if original_len else 1.0
            return _result(structured, ctype, f"bm25_{strategy}", audit_pass, r)

    if mode == "lossless" or original_len < threshold:
        return _result(original, "short", "none", True, 1.0)

    # Stages 1-4 (TS-4): shared structured compression path.
    compressed, content_type, strategy, audit_pass = _structured_compress(
        original, mime_hint=mime_hint, mode=mode)

    ratio = len(compressed) / original_len if original_len else 1.0
    return _result(compressed, content_type, strategy, audit_pass, ratio)


def _generate_tool_summary(tool_name, meta_info):
    """Generate deterministic summary for a cleared tool result.
    Same (tool_name, meta_info) always produces the same output,
    enabling prefix cache hits when the same tool call appears across requests.
    """
    if not tool_name:
        return "tool"
    if meta_info.startswith(" file="):
        return f'{tool_name}("{meta_info[6:]}")'
    elif meta_info.startswith(" cmd="):
        cmd = meta_info[5:].strip()
        return f'{tool_name}("{cmd}")'
    return tool_name


# ============================================================================
# P4 Recall MVP 挂载: ctx_recall error result 拦截 + 改写
# 方案 A(次请求改写)——客户端不认识 ctx_recall 工具, 返回 error result,
# 本函数在消息处理路径中拦截并替换为真实检索结果。
# ============================================================================

def rewrite_ctx_recall_results(messages, session_key):
    """拦截 ctx_recall 的 error tool_result → 替换为真实检索结果。

    流程:
    1. 扫描 assistant 消息建立 tool_use_id → tool_name 映射
    2. 扫描 user 消息的 tool_result, 匹配到 ctx_recall 的 result
    3. 检测 error(客户端不认识工具 → "Error" / "unknown tool" 等)
    4. 从原始 tool_use.input 提取查询参数
    5. 调用 ctx_recall.lookup() + format_recall_result() 获取真实结果
    6. 改写 tool_result 内容

    fail-open: 任何异常静默跳过(不影响正常压缩路径)。
    """
    if not getattr(proxy_state, "PROXY_PD_ENABLED", True) or not session_key:
        return messages

    try:
        # 建立 tool_use_id → (tool_name, input) 映射
        tool_map = {}
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for block in (msg.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_map[block.get("id", "")] = (
                        block.get("name", ""), block.get("input") or {})

        if not any(name == "ctx_recall" for name, _ in tool_map.values()):
            return messages  # 无 ctx_recall 调用, 快速返回

        # 扫描 tool_result 并改写
        from ctx_recall import lookup, format_recall_result, parse_query_offset
        changed = False
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                tid = block.get("tool_use_id", "")
                tool_info = tool_map.get(tid)
                if not tool_info or tool_info[0] != "ctx_recall":
                    continue

                # 检测是否是 error result(客户端不认识工具)
                text = ""
                rc = block.get("content")
                if isinstance(rc, list):
                    text = " ".join(b.get("text", "") for b in rc
                                    if isinstance(b, dict))
                elif isinstance(rc, str):
                    text = rc
                lower = text.lower()
                if "error" not in lower and "unknown" not in lower and \
                   "not found" not in lower and len(text.strip()) >= 20:
                    continue  # 不像 error, 跳过

                # 从 tool_use.input 提取查询参数
                tool_input = tool_info[1]
                query = tool_input.get("query", "")
                kind = tool_input.get("kind")
                limit = tool_input.get("limit", 8)
                if not query:
                    continue

                # 调用真实检索(分页续读: query "r:x@4000" → base+offset)
                base_q, paged_off = parse_query_offset(query)
                results = lookup(session_key, base_q, kind=kind, limit=limit)
                real_text = format_recall_result(results, query,
                                                 session_key=session_key,
                                                 offset=paged_off)

                # 改写 tool_result
                if isinstance(block.get("content"), list):
                    block["content"] = [{"type": "text", "text": real_text}]
                else:
                    block["content"] = real_text
                changed = True

        if changed:
            import proxy_state as _ps_local
            _ps_local._DIAG_ENABLED = _ps_local._DIAG_ENABLED  # no-op, keep reference
    except Exception:
        pass  # fail-open: 改写失败不影响正常路径

    return messages


__all__ = [
    "_scrub_ansi",
    "_detect_content_type",
    "_sieve_json",
    "_compress_code",
    "_compress_log",
    "_compress_text",
    "_dedupe_scalars",
    "_audit_compression",
    "_structured_compress",
    "compress_tool_result",
    "_generate_tool_summary",
    "rewrite_ctx_recall_results",
    # TS-1 BM25 scoring
    "_bm25_tokenize",
    "_bm25_idf",
    "_update_idf",
    "bm25_score_message",
    "_extract_last_user_text",
]
