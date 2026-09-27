#!/usr/bin/env python3
"""Jev MCP server (stdio).

将 Command Code 的 `typesafe/jev` 决策 API 暴露为本地 stdio MCP 工具
`jev_decide`。只依赖 Python 标准库。

协议: newline-delimited JSON-RPC 2.0 over stdio。
工具: `jev_decide(state, questions)`，支持 `noul` / `choice` / `score`，
返回原始概率结构，不生成自由文本结论。

密钥: 从环境变量 `CMD_API_KEY` 读取，以 HTTPS Bearer token 调用
`https://api.commandcode.ai/provider/v1/systemone`。绝不回显 key，
错误信息中不携带响应体原文。
"""

import json
import os
import sys
import urllib.error
import urllib.request

API_URL = "https://api.commandcode.ai/provider/v1/systemone"
MODEL_NAME = "typesafe/jev"
TOOL_NAME = "jev_decide"
SERVER_NAME = "jev"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

MAX_QUESTIONS = 16
MAX_REQUEST_BYTES = 200000
REQUEST_TIMEOUT_S = 60

ALLOWED_TYPES = ("noul", "choice", "score")
ALLOWED_QUESTION_KEYS = frozenset({"type", "instructions", "criteria"})

# 额度探测（2026-09-27 落地，分析见 docs/06-reference-metrics/jev-quota.md）：
# ① 429 时从错误体白名单提取 message——官方语义里它会点名**哪个窗口**与**何时重置**；
#    旧实现把响应体整个丢掉，等于把最有用的信息扔了；
# ② 新增 MCP 工具 `jev_usage`：只读元数据（零内容外发、实测不计次不计费），
#    让调用方在花判定之前就知道「现在能不能调、还能调多少」。
QUOTA_TOOL_NAME = "jev_usage"
MAX_ERROR_BODY_BYTES = 4096
MAX_ERROR_MESSAGE_CHARS = 300


def _load_quota():
    """惰性导入同目录的 jev_quota（脚本方式启动时 sys.path[0] 即 tools/）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import jev_quota
    return jev_quota


def _http_error_detail(exc, api_key):
    """从 HTTP 错误体白名单提取 message（绝不放行响应体其余部分）。"""
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001 - 读不到就算了，不因诊断信息再抛错
        return ""
    if not raw:
        return ""
    msg = ""
    try:
        data = json.loads(raw[:MAX_ERROR_BODY_BYTES].decode("utf-8", "replace"))
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or ""
            msg = msg or data.get("message") or ""
    except ValueError:
        msg = ""
    if not msg:
        return ""
    msg = "".join(ch for ch in msg if ch.isprintable())[:MAX_ERROR_MESSAGE_CHARS]
    return _sanitize_error(msg, api_key)

TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Ask the typesafe/jev decision model about a state. "
        "Returns raw probability structures (noul/choice/score), no free-text verdict."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "state": {
                "description": "The state to judge (string or JSON-serializable object)."
            },
            "questions": {
                "type": "object",
                "description": "Named questions: {name: {type, instructions, criteria?}}.",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["noul", "choice", "score"]},
                        "instructions": {"type": "string"},
                        "criteria": {},
                    },
                    "required": ["type", "instructions"],
                },
            },
        },
        "required": ["state", "questions"],
        "additionalProperties": False,
    },
}

USAGE_TOOL = {
    "name": QUOTA_TOOL_NAME,
    "description": (
        "Read Command Code quota windows (5-hour / weekly) and remaining credits. "
        "Metadata only: no content is sent, no decision is consumed."
    ),
    "inputSchema": {"type": "object", "properties": {},
                    "additionalProperties": False},
}

TOOLS = [TOOL, USAGE_TOOL]


def _validate_request(args):
    """校验工具参数并序列化为官方 API 请求体 (JSON 字符串)。"""
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object with 'state' and 'questions'")
    if "state" not in args or "questions" not in args:
        raise ValueError("arguments must contain 'state' and 'questions'")
    state = args["state"]
    questions = args["questions"]
    if not isinstance(state, (str, dict, list)):
        raise ValueError("'state' must be a string, object, or array")
    if isinstance(state, str) and not state.strip():
        raise ValueError("'state' must not be empty")
    if not isinstance(questions, dict) or not questions:
        raise ValueError("'questions' must be a non-empty object")
    if len(questions) > MAX_QUESTIONS:
        raise ValueError(
            "too many questions: %d > MAX_QUESTIONS=%d" % (len(questions), MAX_QUESTIONS)
        )
    for name, question in questions.items():
        if not isinstance(question, dict):
            raise ValueError("question %r must be an object" % (name,))
        extra = set(question.keys()) - ALLOWED_QUESTION_KEYS
        if extra:
            raise ValueError("question %r has unexpected fields: %s" % (name, sorted(extra)))
        qtype = question.get("type")
        instructions = question.get("instructions")
        if qtype not in ALLOWED_TYPES:
            raise ValueError(
                "question %r has invalid type %r (expected one of %s)"
                % (name, qtype, list(ALLOWED_TYPES))
            )
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("question %r needs non-empty 'instructions'" % (name,))
        criteria = question.get("criteria")
        if qtype == "noul":
            if "criteria" in question:
                raise ValueError("question %r of type 'noul' must not set 'criteria'" % (name,))
        elif qtype == "choice":
            if not isinstance(criteria, dict) or not criteria:
                raise ValueError(
                    "question %r of type 'choice' needs 'criteria' as a non-empty map" % (name,)
                )
        elif qtype == "score":
            if (
                not isinstance(criteria, list)
                or len(criteria) < 2
                or not all(isinstance(item, str) and item.strip() for item in criteria)
            ):
                raise ValueError(
                    "question %r of type 'score' needs 'criteria' as a list of >=2 labels"
                    % (name,)
                )
    payload = {"model": MODEL_NAME, "state": state, "questions": questions}
    body = json.dumps(payload, ensure_ascii=False)
    if len(body.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError(
            "request body exceeds MAX_REQUEST_BYTES=%d" % (MAX_REQUEST_BYTES,)
        )
    return body


def _sanitize_error(message, api_key):
    """从错误文本中剔除 API key，避免回显泄漏。"""
    if api_key and api_key in message:
        message = message.replace(api_key, "[redacted]")
    return message


def _call_jev(args, api_key):
    """调用 Jev 决策 API，返回解析后的 JSON dict。"""
    if not api_key:
        raise RuntimeError("CMD_API_KEY is not set; export it before calling Jev")
    body = _validate_request(args)
    request = urllib.request.Request(
        API_URL,
        data=body.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            # 网关 WAF 会拦截 urllib 默认的 `Python-urllib/x.y` UA，必须显式覆盖。
            "User-Agent": "jev-mcp-server/" + SERVER_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # 绝不回显整包；只保留状态码/原因 + 白名单 message（429 会点名窗口与重置时刻）。
        detail = _http_error_detail(exc, api_key)
        raise RuntimeError("Jev API error: HTTP %s %s%s"
                           % (exc.code, exc.reason, (" — " + detail) if detail else ""))
    except urllib.error.URLError as exc:
        raise RuntimeError(_sanitize_error("Jev API network error: %s" % (exc.reason,), api_key))
    try:
        result = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("Jev API error: invalid JSON response")
    if (
        not isinstance(result, dict)
        or result.get("model") != MODEL_NAME
        or not isinstance(result.get("answers"), dict)
        or not result["answers"]
    ):
        raise RuntimeError("Jev API error: expected decision schema with model/answers")
    return result


def _call_usage(api_key):
    """读 Command Code 额度窗口（元数据只读：不发内容、不消耗判定）。

    密钥取 env → `~/.zshrc` 单源回落（与 jev_decide 不同：额度是元数据、无内容外发，
    故允许在 server 未拿到 env 密钥时回落到本地单一源；jev_decide 仍严格要求 env 密钥）。
    """
    quota = _load_quota()
    key = api_key or quota.load_key()
    if not key:
        raise RuntimeError("CMD_API_KEY is not set (env or ~/.zshrc); cannot read usage")
    try:
        return quota.fetch_usage(key, with_plan=True)
    except quota.QuotaUndetectable as exc:
        # 严格区分「不可探测」与「额度耗尽」——后者是 429 的语义，前者是端点/网络问题。
        raise RuntimeError("quota undetectable (≠ exhausted): %s"
                           % _sanitize_error(str(exc), key))


def _handle_message(message, api_key=""):
    """处理单条 JSON-RPC 消息，返回响应 dict；通知无响应返回 None。"""
    if not isinstance(message, dict):
        return None
    msg_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    def _ok(result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _err(code, text):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}}

    # 无 id 即为通知，按 JSON-RPC 不作响应。
    if "id" not in message:
        return None

    if method == "initialize":
        requested = params.get("protocolVersion", PROTOCOL_VERSION)
        return _ok(
            {
                "protocolVersion": requested,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        )
    if method == "tools/list":
        return _ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in (TOOL_NAME, QUOTA_TOOL_NAME):
            return _err(-32602, "unknown tool: %r" % (name,))
        try:
            api_result = (_call_jev(arguments, api_key) if name == TOOL_NAME
                          else _call_usage(api_key))
        except Exception as exc:  # noqa: BLE001 - 必须转为 MCP tool error
            safe = _sanitize_error(str(exc), api_key)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": "error: " + safe}],
                    "isError": True,
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(api_result, ensure_ascii=False),
                    }
                ],
                "structuredContent": api_result,
                "isError": False,
            },
        }
    if method in ("ping", "notifications/initialized", "notifications/cancelled"):
        return _ok({})
    return _err(-32601, "Method not found: %s" % (method,))


def _serve_stdio(api_key):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue
        response = _handle_message(message, api_key)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def main():
    api_key = os.environ.get("CMD_API_KEY", "")
    _serve_stdio(api_key)


if __name__ == "__main__":
    main()
