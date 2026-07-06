"""TS-3: 统一的压缩结果类型契约.

设计原则:
  - 兼容 Python 3.9 (TypedDict 来自 typing)
  - 不依赖任何第三方包
  - 字段均可 dict 化 (JSON 序列化)

参考: litellm/types/compression.py:15 CompressedResult
"""
import sys

if sys.version_info >= (3, 8):
    from typing import TypedDict, List, Dict, Optional
else:
    from typing_extensions import TypedDict, List, Dict, Optional


class CompressionSubResult(TypedDict, total=False):
    """单条 tool_result 压缩子结果 (compress_tool_result 升级版)."""
    original: str
    compressed: str
    content_type: str
    strategy: str
    audit_pass: bool
    ratio: float
    original_len: int
    compressed_len: int
    bm25_score: Optional[float]
    msg_idx: int
    block_idx: int


class CompressionResult(TypedDict, total=False):
    """统一压缩/截断/清除操作返回类型.

    所有 compress_tool_result / _compress_content_pass / truncate_messages_if_needed /
    clear_old_tool_results / _apply_smart_truncation / _apply_rounds_truncation 统一返回此类型。
    顶级字段全是可选 (total=False),让各调用点按需填字段而不强求全填。
    """
    messages: List[dict]
    original_tokens: int
    compressed_tokens: int
    compression_ratio: float

    strategy: str
    enabled: bool
    skipped: bool
    truncated: bool
    skipped_reason: Optional[str]

    protected_indices: List[int]
    dropped_indices: List[int]
    bm25_scores: Dict[int, float]
    cache_keys: Dict[str, str]

    dropped_messages: int
    kept_messages: int
    compressed_assistants: int
    kept_chars: int
    budget_chars: int

    sub: Dict[str, object]


__all__ = [
    "CompressionResult",
    "CompressionSubResult",
]
