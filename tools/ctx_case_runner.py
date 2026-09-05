#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ctx-case 单步报文调试运行器。

设计: docs/03-experiments-testing/ctx-single-step-case-debug-design-20260905.md
案例: tools/ctx_cases.json（TC01-TC21，规格见 ctx-single-step-case-set-20260905.md）

组件:
  - 影子环境: symlink 仓库 *.py + configs/models.json，真实 logs/ 子目录
    （不放 secret.local.conf、不 source conf → 云端物理不可达，diag 全隔离）
  - 内嵌 mock backend(:8100): 记录 capture JSONL + 按 case steps 序列返回
  - 受控代理(:4100): subprocess 启动影子副本，suite env 决定参数（构造即真相）
  - 断言引擎: 四出口（A响应/B capture/C 影子diag/D 日志）声明式断言
  - 报告: 控制台红绿表 + JSON 报告 + 失败案例问题报文归档

用法:
  python3 tools/ctx_case_runner.py                # 全部套件
  python3 tools/ctx_case_runner.py --case TC01    # 单案例
  python3 tools/ctx_case_runner.py --suite micro  # 单套件
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_ROOT = os.path.join(REPO, "logs", "ctx_case")
CASES_PATH = os.path.join(REPO, "tools", "ctx_cases.json")

PROXY_PORT = 4100
MOCK_PORT = 8100
MOCK_MODEL = "mock-local-35b"
CLIENT_MODEL = "claude-sonnet-4-6"
PROXY_WAIT_S = 25
CASE_TIMEOUT_S = 60

# ---------------------------------------------------------------------------
# 套件 env（构造即真相；均为直接 env，不 source conf）
# ---------------------------------------------------------------------------

BASE_ENV = {
    "PORT": str(PROXY_PORT),
    "LLAMA_BASE_URL": "http://127.0.0.1:%d/v1" % MOCK_PORT,
    "MODEL_NAME": MOCK_MODEL,
    "PROXY_DIAG_ENABLED": "true",
    "PROXY_QUEUE_ENABLED": "false",
    "PROXY_HBE_ENABLED": "false",
    "PROXY_TOOL_FILTER_ENABLED": "true",
    "PROXY_TOOL_FILTER_MAX": "14",
    "PROXY_PD_ENABLED": "true",
    "PROXY_CTX_KEEP_MESSAGES": "80",
    "PROXY_COMPRESS_MIN_CHARS": "999999",   # 各套件默认关压缩保确定性
    "PYTHONUNBUFFERED": "1",
}

SUITES = {
    "prod": {"PROXY_CTX_ENGINE_ENABLED": "true",
             "PROXY_PD_MICRO_TURN_ENABLED": "false"},
    "micro": {"PROXY_CTX_ENGINE_ENABLED": "true",
              "PROXY_PD_MICRO_TURN_ENABLED": "true"},
    # 413 门实测位于 _do_dispatch（管线后），用小字节上限让 150KB 报文
    # 活着穿过引擎走到派发门；epoch 预算放大防 mock 模型（无目录 ctx）
    # auto 预算过小导致引擎先溢出 500
    "bigbody": {"PROXY_CTX_ENGINE_ENABLED": "true",
                "PROXY_PD_MICRO_TURN_ENABLED": "false",
                "PROXY_MAX_REQUEST_BYTES": "102400",
                "PROXY_CTX_EPOCH_TRIGGER_TOKENS": "1000000"},
    # PRE_TRUNCATE 保持默认（管线前体级裁剪不登记 manifest）——登记路径
    # 须由 stage 14/17 触发；lifecycle 相位由 chars 阈值阶梯驱动，全部
    # 压低使 ~2.3KB 载荷直达 pre_trunc（truncate_rounds+oom_safety 开启）
    "engineoff": {"PROXY_CTX_ENGINE_ENABLED": "false",
                  "PROXY_PD_MICRO_TURN_ENABLED": "false",
                  "PROXY_CTX_KEEP_MESSAGES": "6",
                  "PROXY_OOM_SAFE_TOKENS": "100",
                  "PROXY_CLEAR_THRESHOLD": "200",
                  "PROXY_CHARS_GROWTH": "400",
                  "PROXY_CHARS_EXPANSION": "600",
                  "PROXY_CHARS_SATURATION": "800",
                  "PROXY_CHARS_OOM_DANGER": "1000"},
    "epoch": {"PROXY_CTX_ENGINE_ENABLED": "true",
              "PROXY_PD_MICRO_TURN_ENABLED": "false",
              "PROXY_CTX_EPOCH_TRIGGER_TOKENS": "2000",
              "PROXY_CTX_WINDOW_K": "4"},
    # engine-on + 微轮自答：单请求内验证"折叠→登记→召回→回填"全链
    "epochmicro": {"PROXY_CTX_ENGINE_ENABLED": "true",
                   "PROXY_PD_MICRO_TURN_ENABLED": "true",
                   "PROXY_CTX_EPOCH_TRIGGER_TOKENS": "2000",
                   "PROXY_CTX_WINDOW_K": "4"},
}

# ---------------------------------------------------------------------------
# Mock backend
# ---------------------------------------------------------------------------

class _MockHandler(BaseHTTPRequestHandler):
    server_version = "ctxcase-mock/1"

    def log_message(self, *a):  # 静默
        pass

    def _reply(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._reply(200, {"object": "list", "data": [
                {"id": MOCK_MODEL, "object": "model", "owned_by": "ctxcase"}]})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            self._reply(404, {"error": "not found"})
            return
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            req = {}
        mock = self.server.ctxcase
        with mock.lock:
            with open(mock.capture_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(),
                                    "headers": dict(self.headers),
                                    "body": req}, ensure_ascii=False) + "\n")
            step = None
            if mock.steps:
                step = mock.steps.pop(0)
        if step is None:
            step = {"text": "ok"}
        msg = {"role": "assistant", "content": step.get("text")}
        finish = "stop"
        if step.get("tool_calls"):
            tcs = []
            for i, tc in enumerate(step["tool_calls"]):
                tcs.append({
                    "id": tc.get("id") or "call_ctxcase_%d" % i,
                    "type": "function",
                    "function": {"name": tc["name"],
                                 "arguments": json.dumps(
                                     tc.get("arguments", {}),
                                     ensure_ascii=False)}})
            msg["tool_calls"] = tcs
            msg["content"] = step.get("text")
            finish = "tool_calls"
        self._reply(200, {
            "id": "chatcmpl-ctxcase", "object": "chat.completion",
            "created": int(time.time()), "model": req.get("model", MOCK_MODEL),
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


class MockBackend(object):
    def __init__(self, capture_path):
        self.capture_path = capture_path
        self.steps = []
        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), _MockHandler)
        self.httpd.ctxcase = self
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def set_steps(self, steps):
        with self.lock:
            self.steps = list(steps or [])

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------------------------------------------------------------------------
# 影子环境 + 受控代理
# ---------------------------------------------------------------------------

def build_shadow(root):
    shadow = os.path.join(root, "shadow")
    os.makedirs(shadow)
    # 复制而非 symlink——本机 Python 3.9 import 机制会把 symlink 模块的
    # __file__ 解析回真实路径，_SCRIPT_DIR 隔离失效（首跑教训，设计 §9）
    for fn in os.listdir(REPO):
        if fn.endswith(".py"):
            shutil.copy2(os.path.join(REPO, fn), os.path.join(shadow, fn))
    cfg = os.path.join(shadow, "configs")
    os.makedirs(cfg)
    shutil.copy2(os.path.join(REPO, "configs", "models.json"),
                 os.path.join(cfg, "models.json"))
    for sub in ("", "manifest", "orig", "index", "archive", "ledger"):
        os.makedirs(os.path.join(shadow, "logs", "diag", sub), exist_ok=True)
    return shadow


class ProxyProcess(object):
    def __init__(self, shadow, env_overrides, log_path):
        env = dict(os.environ)
        env.update(BASE_ENV)
        env.update(env_overrides)
        for k in ("LLAMA_API_KEY", "ZHIPU_API_KEY", "KIMI_API_KEY",
                  "ANTHROPIC_API_KEY"):
            env.pop(k, None)
        self.log_path = log_path
        self.log_fh = open(log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(shadow, "anthropic_proxy.py")],
            cwd=shadow, env=env, stdout=self.log_fh, stderr=subprocess.STDOUT)

    def wait_ready(self):
        deadline = time.time() + PROXY_WAIT_S
        url = "http://127.0.0.1:%d/api/status" % PROXY_PORT
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("proxy exited early, see %s" % self.log_path)
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    d = json.loads(r.read())
                if (d.get("proxy") or {}).get("alive"):
                    return True
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.4)
        raise RuntimeError("proxy not ready in %ss, see %s"
                           % (PROXY_WAIT_S, self.log_path))

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log_fh.close()


# ---------------------------------------------------------------------------
# 报文构造辅助（body 占位符展开）
# ---------------------------------------------------------------------------

def dummy_tool(i):
    return {"name": "DummyTool%02d" % i,
            "description": "ctxcase 占位工具 %d" % i,
            "input_schema": {"type": "object", "properties": {}}}


CTX_RECALL_SCHEMA = {
    "name": "ctx_recall",
    "description": "查询本会话被折叠的历史上下文（渐进披露拉取接口）",
    "input_schema": {"type": "object", "properties": {
        "query": {"type": "string"},
        "kind": {"type": "string"},
        "limit": {"type": "integer"}}},
}


def expand_tools(spec):
    """{"$dummy": N, "$ctx_recall": true, "$standard": M} → 工具定义列表。"""
    if not isinstance(spec, dict):
        return spec
    tools = []
    n = spec.get("$dummy", 0)
    tools += [dummy_tool(i) for i in range(n)]
    # 标准工具名（命中 TOOL_ALWAYS_KEEP，使过滤护栏不误伤注入——TC01 缺陷的绕行）
    for name in (spec.get("$standard") or []):
        tools.append({"name": name, "description": "%s tool" % name,
                      "input_schema": {"type": "object", "properties": {}}})
    if spec.get("$ctx_recall"):
        tools.append(dict(CTX_RECALL_SCHEMA))
    return tools


def expand_body(body):
    body = json.loads(json.dumps(body))  # deep copy
    if isinstance(body.get("tools"), dict):
        body["tools"] = expand_tools(body["tools"])
    if "$big" in body:
        n = int(body.pop("$big"))
        body["messages"] = [{"role": "user",
                             "content": "x" * n}]
    turns = body.pop("$turns", None)
    if turns:
        tag = body.pop("$turn_tag", "turn")
        tpl = body.pop("$turn_text", None)
        fact = body.pop("$fact", None)  # {"turn_index": N, "text": "..."}
        msgs = []
        for i in range(int(turns)):
            role = "user" if i % 2 == 0 else "assistant"
            text = (tpl % i) if tpl else (
                "%s-%03d: read /repo/src/module_%03d.py "
                "and check function parse_value_%03d" % (tag, i, i, i))
            msgs.append({"role": role, "content": text})
        if fact:
            fi = int(fact.get("turn_index", 10))
            fi = min(max(fi, 0), len(msgs) - 5)  # 留尾窗，确保进折叠区
            # 注入真实形态：assistant(tool_use Read) + user(tool_result 含事实)
            # ——事实活在工具结果里才有 r: 锚与 archive 恢复路径（h: 纯文本
            #   锚实测不可恢复，见 TC23 首轮发现）
            msgs[fi] = {"role": "assistant", "content": [{
                "type": "tool_use", "id": "call_factprobe",
                "name": "Read",
                "input": {"file_path": "/repo/ops/deploy.md"}}]}
            msgs[fi + 1] = {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "call_factprobe",
                "content": fact["text"]}]}
        body["messages"] = msgs
    if body.get("model") == "$client":
        body["model"] = CLIENT_MODEL
    return body


# ---------------------------------------------------------------------------
# 断言引擎
# ---------------------------------------------------------------------------

def _json_substring(needle, blob):
    return needle in blob


def eval_expect(expect, resp_status, resp_headers, resp, captures,
                log_text, diag_dir):
    """返回 [(断言名, 期望, 实际, ok)]。"""
    out = []

    def add(name, exp, actual, ok):
        out.append((name, json.dumps(exp, ensure_ascii=False)[:90],
                    json.dumps(actual, ensure_ascii=False)[:160], bool(ok)))

    if "resp_status" in expect:
        add("resp_status", expect["resp_status"], resp_status,
            resp_status == expect["resp_status"])
    for name, substr in (expect.get("resp_header") or {}).items():
        actual = resp_headers.get(name.lower(), "")
        add("resp_header.%s" % name, substr, actual, substr in actual)
    if "resp_stop_reason" in expect:
        actual = (resp or {}).get("stop_reason")
        add("resp_stop_reason", expect["resp_stop_reason"], actual,
            actual == expect["resp_stop_reason"])
    if "resp_tool_use_names" in expect:
        names = [b.get("name") for b in (resp or {}).get("content", [])
                 if isinstance(b, dict) and b.get("type") == "tool_use"]
        spec = expect["resp_tool_use_names"]
        if "exact" in spec:
            add("resp_tool_use_names.exact", spec["exact"], names,
                names == spec["exact"])
        for n in spec.get("contains", []):
            add("resp_tool_use_names.contains", n, names, n in names)
    for s in expect.get("resp_text_contains", []):
        text = " ".join(b.get("text", "") for b in (resp or {}).get("content", [])
                        if isinstance(b, dict) and b.get("type") == "text")
        add("resp_text_contains", s, text[:80], s in text)

    if "dispatch_count" in expect:
        add("dispatch_count", expect["dispatch_count"], len(captures),
            len(captures) == expect["dispatch_count"])
    if captures:
        last = captures[-1]
        blob = json.dumps(last.get("body", {}), ensure_ascii=False)
        for s in expect.get("last_dispatch_contains", []):
            add("last_dispatch_contains", s, "(%.250s)" % blob[:250],
                _json_substring(s, blob))
        for s in expect.get("last_dispatch_not_contains", []):
            add("last_dispatch_not_contains", s, s,
                not _json_substring(s, blob))
        bt = expect.get("backend_tools_last")
        if bt:
            tools = last.get("body", {}).get("tools") or []
            names = [t.get("function", {}).get("name") for t in tools
                     if isinstance(t, dict)]
            if "count_max" in bt:
                add("backend_tools.count_max", bt["count_max"], len(names),
                    len(names) <= bt["count_max"])
            if "count_exact" in bt:
                add("backend_tools.count_exact", bt["count_exact"], len(names),
                    len(names) == bt["count_exact"])
            if "last" in bt:
                add("backend_tools.last", bt["last"], names[-1:] or [],
                    bool(names) and names[-1] == bt["last"])
            for n in bt.get("contains", []):
                add("backend_tools.contains", n, names, n in names)
            for name, cnt in (bt.get("count_name_eq") or {}).items():
                got = names.count(name)
                add("backend_tools.count_eq[%s]" % name, cnt, got,
                    got == cnt)
        if "backend_model_last" in expect:
            actual = last.get("body", {}).get("model")
            add("backend_model_last", expect["backend_model_last"], actual,
                actual == expect["backend_model_last"])
        if "backend_msg_count_max" in expect:
            n = len(last.get("body", {}).get("messages") or [])
            add("backend_msg_count_max", expect["backend_msg_count_max"], n,
                n <= expect["backend_msg_count_max"])
        if "backend_msg_count_min" in expect:
            n = len(last.get("body", {}).get("messages") or [])
            add("backend_msg_count_min", expect["backend_msg_count_min"], n,
                n >= expect["backend_msg_count_min"])

    for s in expect.get("log_contains", []):
        add("log_contains", s, "(log %d chars)" % len(log_text),
            s in log_text)
    for s in expect.get("log_not_contains", []):
        add("log_not_contains", s, "(log %d chars)" % len(log_text),
            s not in log_text)

    for spec in expect.get("manifest_count_min", []) or []:
        rows = _read_manifest(diag_dir, spec["session"])
        add("manifest_count_min[%s]" % spec["session"],
            ">= %s" % spec.get("min", 1), len(rows),
            len(rows) >= spec.get("min", 1))
    for spec in expect.get("manifest_anchor_max_dup", []) or []:
        rows = _read_manifest(diag_dir, spec["session"])
        counts = {}
        for r in rows:
            a = r.get("anchor") or ""
            counts[a] = counts.get(a, 0) + 1
        worst = max(counts.values()) if counts else 0
        add("manifest_anchor_max_dup[%s]" % spec["session"],
            "<= %s" % spec.get("max", 1),
            "worst anchor dup=%d (%d rows)" % (worst, len(rows)),
            worst <= spec.get("max", 1))
    for spec in expect.get("manifest_field_contains", []) or []:
        rows = _read_manifest(diag_dir, spec["session"])
        toks = [t.lower() for t in spec.get("any_of", [])]
        hit = any(any(t in (r.get(spec.get("field"), "") or "").lower()
                      for t in toks) for r in rows)
        add("manifest_field_contains[%s.%s]" % (spec["session"],
                                                spec.get("field")),
            spec.get("any_of"), "hit=%s over %d rows" % (hit, len(rows)), hit)
    return out


def _read_manifest(diag_dir, session):
    path = os.path.join(diag_dir, "manifest", "%s.jsonl" % session)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    return rows


# ---------------------------------------------------------------------------
# 案例执行
# ---------------------------------------------------------------------------

def send_case_request(case, body):
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "X-Claude-Code-Session-Id": case["session"],
        "X-Proxy-Route-To": "local",
    }
    headers.update(case.get("headers") or {})
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/messages" % PROXY_PORT,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=CASE_TIMEOUT_S) as r:
            return r.status, dict(r.headers), json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except (ValueError, json.JSONDecodeError):
            payload = {}
        return e.code, dict(e.headers), payload


def read_captures(path, offset):
    if not os.path.exists(path):
        return offset, []
    with open(path, encoding="utf-8") as f:
        f.seek(offset)
        rows = []
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
        return f.tell(), rows


def seed_manifest(shadow, session, rows):
    path = os.path.join(shadow, "logs", "diag", "manifest",
                        "%s.jsonl" % session)
    now = datetime.now().isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            # ts 一律注入当前时间——ManifestStore._rotate_stale_file 按末行
            # ts 判批次，硬编码旧 ts 会在首次读取时被轮转清空（首跑教训）
            f.write(json.dumps(dict(r, ts=now), ensure_ascii=False) + "\n")


def expand_bulk_seed(spec):
    """$bulk: {"keyword","interference","target_head"} → 干扰行+目标行。"""
    kw = spec.get("keyword", "bulkkw")
    n = int(spec.get("interference", 20))
    rows = []
    for i in range(n):
        rows.append({
            "turn": 1, "reason": "fifo_drop",
            "anchor": "r:bulk%03d" % i, "kind": "tool_result",
            "role": "user", "tool": "Read",
            "handle": {"type": "path", "value": "/repo/bulk_%03d.py" % i},
            "size_chars": 200,
            "head": "interference-%03d %s filler text block" % (i, kw),
            "triggers": kw, "ts": "2026-09-05T00:00:00"})
    rows.append({
        "turn": 2, "reason": "fifo_drop", "anchor": "r:bulktarget",
        "kind": "tool_result", "role": "user", "tool": "Read",
        "handle": {"type": "path", "value": "/repo/target.py"},
        "size_chars": 200,
        "head": spec.get("target_head", "TARGET-UNIQUE %s goal" % kw),
        "triggers": kw, "ts": "2026-09-05T00:00:01"})
    return rows


def run_case(case, mock, shadow, log_path):
    session = case["session"]
    capture_path = mock.capture_path
    cap_off, _ = read_captures(capture_path, 0)
    with open(log_path, encoding="utf-8", errors="replace") as f:
        log_off = f.seek(0, 2)

    mock.set_steps(case.get("mock_steps"))
    for spec in case.get("seed_manifest") or []:
        rows = spec.get("rows")
        if spec.get("$bulk"):
            rows = expand_bulk_seed(spec["$bulk"])
        seed_manifest(shadow, spec["session"], rows)

    resp_status, resp_headers, resp = 0, {}, None
    bodies = case.get("bodies")
    if bodies:
        seq = [expand_body(b) for b in bodies]
    else:
        seq = [expand_body(case.get("body") or {})] * int(
            case.get("repeat", 1))
    # 会话唯一 nonce：避开代理 2s body-hash 去重层（TC16 重发语义另用
    # repeat_delay 拉开间隔，保持 body 逐字节一致）
    nonce = "" if case.get("no_nonce") else "\n[ctxcase %s]" % case["id"]
    for body in seq:
        if nonce and body.get("messages"):
            first = body["messages"][0]
            if isinstance(first.get("content"), str):
                first["content"] += nonce
    delay = float(case.get("repeat_delay", 0))
    for idx, body in enumerate(seq):
        if idx and delay:
            time.sleep(delay)
        resp_status, resp_headers, resp = send_case_request(case, body)
    resp_headers = {k.lower(): v for k, v in (resp_headers or {}).items()}
    time.sleep(0.2)

    _, captures = read_captures(capture_path, cap_off)
    with open(log_path, encoding="utf-8", errors="replace") as f:
        f.seek(log_off)
        log_text = f.read()
    diag_dir = os.path.join(shadow, "logs", "diag")
    results = eval_expect(case.get("expect") or {}, resp_status,
                          resp_headers, resp, captures, log_text, diag_dir)
    ok = all(r[3] for r in results)
    return {"id": case["id"], "name": case.get("name", ""), "suite":
            case["suite"], "ok": ok, "asserts": results,
            "request": case.get("body") or case.get("bodies"),
            "expanded_request": seq,
            "response": resp, "response_status": resp_status,
            "captures": captures, "log_excerpt": log_text[-4000:]}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", action="append", default=None,
                    help="只跑指定案例 id（可多次）")
    ap.add_argument("--suite", action="append", default=None,
                    help="只跑指定套件")
    ap.add_argument("--real-backend", action="store_true",
                    help="LLAMA_BASE_URL 指向真实 :8081（行为探针模式；"
                         "仍走影子代理，隔离不变，captures 为空）")
    args = ap.parse_args()

    with open(CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)
    if args.case:
        want = set(args.case)
        cases = [c for c in cases if c["id"] in want]
    if args.suite:
        want = set(args.suite)
        cases = [c for c in cases if c["suite"] in want]
    # requires 门：未满足运行条件的案例跳过而非假失败（如 real_backend）
    _skipped = [c for c in cases
                if c.get("requires") == "real_backend" and not args.real_backend]
    if _skipped:
        cases = [c for c in cases if c not in _skipped]
        print("跳过（需 --real-backend）:",
              ", ".join(c["id"] for c in _skipped))

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = os.path.join(RUN_ROOT, "run-%s" % ts)
    os.makedirs(root, exist_ok=True)
    shadow = build_shadow(root)
    capture_path = os.path.join(root, "mock_capture.jsonl")
    mock = MockBackend(capture_path)

    # 行为探针模式：LLAMA_BASE_URL 指向真实后端（MODEL_NAME 同步换成真模型码）
    real_env = {}
    if args.real_backend:
        real_env = {
            "LLAMA_BASE_URL": "http://127.0.0.1:8081/v1",
            "MODEL_NAME": "pyros-vault/Ornith-1.5-35B-A3B-oQ4e-fixed-mtp",
        }
        BASE_ENV.update(real_env)

    report = {"ts": ts, "cases": [], "pass": 0, "fail": 0,
              "real_backend": bool(args.real_backend)}
    current_suite = None
    proxy = None
    try:
        for case in cases:
            if case["suite"] != current_suite:
                if proxy:
                    proxy.stop()
                suite_env = dict(BASE_ENV)
                suite_env.update(SUITES[case["suite"]])
                log_path = os.path.join(root, "proxy-%s.log" % case["suite"])
                proxy = ProxyProcess(shadow,
                                     {k: v for k, v in suite_env.items()
                                      if k not in BASE_ENV}, log_path)
                proxy.wait_ready()
                current_suite = case["suite"]
                print("== suite %s ==" % case["suite"])
            r = run_case(case, mock, shadow,
                         os.path.join(root, "proxy-%s.log" % case["suite"]))
            report["cases"].append(r)
            mark = "PASS" if r["ok"] else "FAIL"
            report["pass" if r["ok"] else "fail"] += 1
            print("[%s] %s %s — %s" % (mark, r["id"], r["name"],
                  "; ".join("%s %s" % (a[0], "ok" if a[3] else "MISMATCH")
                            for a in r["asserts"] if not a[3]) or "all ok"))
            if not r["ok"]:
                fail_path = os.path.join(root, "fail-%s.json" % case["id"])
                with open(fail_path, "w", encoding="utf-8") as f:
                    json.dump(r, f, ensure_ascii=False, indent=1)
    finally:
        if proxy:
            proxy.stop()
        mock.stop()

    with open(os.path.join(root, "report.json"), "w",
              encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("\n== 汇总: PASS %d / FAIL %d → %s ==" % (report["pass"],
                                                    report["fail"], root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
