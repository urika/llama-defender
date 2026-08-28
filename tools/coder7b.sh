#!/bin/bash
# ============================================================
# coder7b.sh — Qwen2.5-Coder-7B-Instruct-8bit IDE 补全服务启停
# 独立于生产栈(8081 Ornith / 4000 llama-defender), 仅供补全直连。
# 用法:
#   tools/coder7b.sh start    # 起服务(8083, 读 configs/qwen25-coder-7b-8bit.conf)
#   tools/coder7b.sh stop     # 停服务(Ornith 批跑前必做)
#   tools/coder7b.sh status   # 端口/进程/身份/FIM 探针
#   tools/coder7b.sh probe    # 只跑 FIM 探针
# 设计约束: 不动 active.conf / 不动生产进程 / stdlib 工具即可运行
# ============================================================
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF="$ROOT/configs/qwen25-coder-7b-8bit.conf"
PIDFILE="$ROOT/.coder7b.pid"
LOGFILE="$ROOT/logs/coder7b-8083.log"
PORT=8083
MODEL="mlx-community/Qwen2.5-Coder-7B-Instruct-8bit"

pid_alive() {
  [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

cmd_start() {
  if pid_alive; then
    echo "已在运行 (PID $(cat "$PIDFILE"))"; exit 0
  fi
  if lsof -ti "tcp:$PORT" >/dev/null 2>&1; then
    echo "端口 $PORT 被占用: $(lsof -ti "tcp:$PORT" | tr '\n' ' ')"
    exit 1
  fi
  # 从 conf 提取启动参数(不 source 整个文件, 避免触碰 active.conf 语义)
  local model gpu cache
  model=$(grep -E '^LLAMA_MODEL=' "$CONF" | head -1 | sed 's/^LLAMA_MODEL=//; s/"//g')
  gpu=$(grep -oE '\-\-gpu-memory-utilization[[:space:]]+[0-9.]+' "$CONF" | grep -oE '[0-9.]+$')
  cache=$(grep -oE '\-\-cache-memory-mb[[:space:]]+[0-9]+' "$CONF" | grep -oE '[0-9]+$')
  HF_HUB_OFFLINE=1 nohup rapid-mlx serve "$model" \
    --host 127.0.0.1 --port "$PORT" \
    --log-level INFO --enable-prefix-cache \
    --no-mllm --gpu-memory-utilization "${gpu:-0.22}" \
    --cache-memory-mb "${cache:-1536}" --max-num-seqs 1 \
    >> "$LOGFILE" 2>&1 &
  local pid=$!
  echo "$pid" > "$PIDFILE"
  echo "启动中 (PID $pid), 等待就绪..."
  for _ in $(seq 1 60); do
    if curl -s -m 2 "http://127.0.0.1:$PORT/v1/models" 2>/dev/null | grep -q "Coder-7B"; then
      echo "就绪: http://127.0.0.1:$PORT  (FIM: POST /v1/completions)"
      exit 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "启动失败, 日志尾部:"; tail -5 "$LOGFILE"; rm -f "$PIDFILE"; exit 1
    fi
    sleep 2
  done
  echo "60s 未就绪, 查日志: $LOGFILE"; exit 1
}

cmd_stop() {
  if pid_alive; then
    local pid; pid=$(cat "$PIDFILE")
    kill "$pid"
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    rm -f "$PIDFILE"
    echo "已停止 (PID $pid)"
  else
    rm -f "$PIDFILE"; echo "未在运行"
  fi
}

cmd_probe() {
  python3 - <<'PY'
import http.client, json
conn = http.client.HTTPConnection("127.0.0.1", 8083, timeout=60)
body = {"prompt": "<|fim_prefix|>def clamp(v, lo, hi):\n    \"\"\"Clamp v into [lo, hi].\"\"\"\n<|fim_suffix|>\n    return clamped\n<|fim_middle|>",
        "max_tokens": 48, "temperature": 0, "stop": ["<|fim_end|>", "<|endoftext|>"]}
conn.request("POST", "/v1/completions", json.dumps(body), {"Content-Type": "application/json"})
r = conn.getresponse(); d = json.loads(r.read().decode()); conn.close()
text = d["choices"][0]["text"].strip()
ok = "clamped" in text and ("min" in text or "max" in text)
print(("FIM 探针 OK: " if ok else "FIM 探针异常: ") + repr(text[:100]))
exit(0 if ok else 1)
PY
}

cmd_status() {
  if pid_alive; then
    echo "进程: PID $(cat "$PIDFILE") 运行中"
  else
    echo "进程: 未运行"
  fi
  if lsof -ti "tcp:$PORT" >/dev/null 2>&1; then
    echo "端口 $PORT: 监听中"
    curl -s -m 3 "http://127.0.0.1:$PORT/v1/models" | grep -o '"id":"[^"]*"' | head -1
  else
    echo "端口 $PORT: 未监听"
  fi
}

case "${1:-}" in
  start) cmd_start ;;
  stop)  cmd_stop ;;
  status) cmd_status ;;
  probe) cmd_probe ;;
  *) echo "用法: $0 {start|stop|status|probe}"; exit 1 ;;
esac
