#!/usr/bin/env bash
# ============================================================
# activate_fnf_flags.sh — fnf 一期四旗标的「一次窗一次戳」激活器
#
#   F1  PROXY_FNF_GUIDE_ENABLED
#   F2  PROXY_FNF_CANDIDATES_ENABLED
#   F3' PROXY_LOOP_ESCALATION_MEMORY_ENABLED
#   F3  PROXY_LOOP_BASH_PATH_KEYS_ENABLED（兜底面）
#
# 纪律（swe-eval 方案件 §3.2 + 协调件 §五 1027-1）：
#   · 缺省 DRY-RUN——必须显式 --apply 才真正改 conf + reload
#   · 激活前必须**空窗**：无在飞批跑/影子探针，代理静默 ≥ IDLE_SEC 秒
#   · 一次窗一次戳：conf sha256 + 窗前后 /metrics 快照 + 代理 PID 落档
#   · 激活后做**功能探针**（3 次同路径 fnf → 日志出现 fnf_guided=1），
#     证明旗标确已生效（reload 只改进程内属性，ps 看不到）
#   · 一切留档 logs/ops/fnf-activation-<ts>/，供 swe-eval 判读对账
#
# 用法:
#   tools/activate_fnf_flags.sh                 # DRY-RUN：只做检查与预演
#   tools/activate_fnf_flags.sh --apply         # 真激活（需空窗）
#   tools/activate_fnf_flags.sh --apply --only-guide   # 只开 F1（最小面）
#   tools/activate_fnf_flags.sh --status         # 看当前四旗标状态
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CONF_LINK="configs/active.conf"
CONF_TARGET="$(readlink "$CONF_LINK" 2>/dev/null || echo "$CONF_LINK")"
case "$CONF_TARGET" in
  /*) CONF_PATH="$CONF_TARGET"; CONF="$CONF_TARGET";;
  */*) CONF_PATH="$ROOT/$CONF_TARGET"; CONF="$CONF_TARGET";;
  *)   CONF_PATH="$ROOT/configs/$CONF_TARGET"; CONF="configs/$CONF_TARGET";;
esac
PROXY_URL="http://127.0.0.1:4000"
METRICS_URL="http://127.0.0.1:8081/metrics"
IDLE_SEC="${FNF_IDLE_SEC:-30}"
APPLY=0
ONLY=""
STAMP_DIR=""

info()  { printf '\033[0;32m[INFO]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
error() { printf '\033[0;31m[ERROR]\033[0m %s\n' "$*"; }
die()   { error "$*"; exit 1; }

FLAG_KEYS=(
  "PROXY_FNF_GUIDE_ENABLED"
  "PROXY_FNF_CANDIDATES_ENABLED"
  "PROXY_LOOP_ESCALATION_MEMORY_ENABLED"
  "PROXY_LOOP_BASH_PATH_KEYS_ENABLED"
)

# ------------------------------------------------------------
# 解析参数
# ------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift;;
    --only-guide) ONLY="guide"; shift;;
    --status) ONLY="status"; shift;;
    -h|--help) sed -n '2,25p' "$0"; exit 0;;
    *) die "未知参数: $1（--apply / --only-guide / --status）";;
  esac
done

# ------------------------------------------------------------
# 当前状态
# ------------------------------------------------------------
show_status() {
  echo "当前激活配置: $CONF"
  for k in "${FLAG_KEYS[@]}"; do
    local v
    v="$(grep -E "^export ${k}=" "$CONF_PATH" 2>/dev/null | tail -1 | sed 's/^export //; s/=//; s/"//g' || true)"
    printf '  %-40s %s\n' "$k" "${v:-（未在 conf 中 → 取默认值 false）}"
  done
  local pid
  pid="$(pgrep -f 'anthropic_proxy.py' | head -1 || true)"
  echo "  代理 PID: ${pid:-未运行}"
}

if [[ "$ONLY" == "status" ]]; then
  show_status
  exit 0
fi

# ------------------------------------------------------------
# 前置检查（空窗门）
# ------------------------------------------------------------
info "=== 前置检查 ==="
[[ -f "$CONF_PATH" ]] || die "激活配置不存在: $CONF_PATH"

proxy_pid="$(pgrep -f 'anthropic_proxy.py' | head -1 || true)"
[[ -n "$proxy_pid" ]] || die "代理未运行（:4000）——先 ./manage.sh start"

# 在飞批跑 / 影子探针
BUSY=""
for pat in "run_agent.py" "swe_test exp" "exp _exec" "shadow"; do
  if pgrep -f "$pat" >/dev/null 2>&1; then
    # 排除本脚本自身与 grep 噪声
    if pgrep -f "$pat" | grep -qv "$$"; then
      BUSY="$BUSY $pat"
    fi
  fi
done
if [[ -n "$BUSY" ]]; then
  warn "检测到在飞批跑/探针相关进程:$BUSY —— 空窗未开，不激活"
  warn "（swe-eval 影子探针=shadow_probes 写入中时亦属在飞；请与对端确认后再跑）"
  [[ "$APPLY" == "1" ]] && die "空窗门未过，拒绝激活"
else
  info "空窗门（进程面）通过：无 run_agent.py / swe_test exp / shadow 进程"
fi

# 代理静默检查（最近一次请求距今 ≥ IDLE_SEC）
last_req_ts="$(grep -a "^\[[0-9:]*\].*REQ_SUMMARY" logs/anthropic_proxy.log 2>/dev/null | tail -1 | sed -n 's/^\[\([0-9:]*\)\].*/\1/p' || true)"
if [[ -n "$last_req_ts" ]]; then
  idle=$(( $(date +%s) - $(date -j -f "%H:%M:%S" "$last_req_ts" +%s 2>/dev/null || echo 0) ))
  if [[ "$idle" -lt "$IDLE_SEC" ]]; then
    warn "代理最近 ${idle}s 前仍有请求（门限 ${IDLE_SEC}s）—— 空窗未开"
    [[ "$APPLY" == "1" ]] && die "静默门未过，拒绝激活"
  else
    info "静默门通过：最近请求距今 ${idle}s"
  fi
else
  info "静默门：日志无 REQ_SUMMARY 记录（视为通过）"
fi

# ------------------------------------------------------------
# 落档目录 + 窗前快照
# ------------------------------------------------------------
TS="$(date +%Y%m%d-%H%M%S)"
STAMP_DIR="$ROOT/logs/ops/fnf-activation-$TS"
mkdir -p "$STAMP_DIR"

sha_before="$(shasum -a 256 "$CONF_PATH" | awk '{print $1}')"
curl -s --max-time 10 "$METRICS_URL" > "$STAMP_DIR/metrics-pre.txt" 2>/dev/null || true
curl -s --max-time 10 "$PROXY_URL/api/status" > "$STAMP_DIR/status-pre.json" 2>/dev/null || true
cp "$CONF_PATH" "$STAMP_DIR/conf-before.conf"
printf '%s\n' "$sha_before" > "$STAMP_DIR/conf-sha256-before.txt"
info "窗前快照 → $STAMP_DIR"
info "  conf sha256(before) = $sha_before"
info "  代理 PID = $proxy_pid"

# ------------------------------------------------------------
# 改 conf（幂等）
# ------------------------------------------------------------
set_flag() {
  local key="$1" val="$2"
  if grep -qE "^export ${key}=" "$CONF_PATH"; then
    # 已有行 → 就地替换
    python3 - "$CONF_PATH" "$key" "$val" <<'PY'
import io, re, sys
p, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
t = io.open(p, encoding="utf-8").read()
t = re.sub(r'^export %s=.*$' % re.escape(key),
           'export %s="%s"' % (key, val), t, count=1, flags=re.M)
io.open(p, "w", encoding="utf-8").write(t)
PY
  else
    printf '\nexport %s="%s"\n' "$key" "$val" >> "$CONF_PATH"
  fi
}

if [[ "$APPLY" != "1" ]]; then
  echo
  info "DRY-RUN（未改动任何文件）。--apply 时将执行："
  for k in "${FLAG_KEYS[@]}"; do
    if [[ "$ONLY" == "guide" && "$k" != "PROXY_FNF_GUIDE_ENABLED" ]]; then
      echo "  - 保持 $k 原值（--only-guide）"
    else
      echo "  - set $k=true"
    fi
  done
  echo "  - ./manage.sh reload"
  echo "  - 功能探针（3× 同路径 fnf → 日志须出现 fnf_guided=1）"
  echo "  - 窗后快照 → $STAMP_DIR"
  exit 0
fi

info "=== 应用（一次窗一次戳）==="
if [[ "$ONLY" == "guide" ]]; then
  set_flag "PROXY_FNF_GUIDE_ENABLED" "true"
  info "仅开 F1（--only-guide）：PROXY_FNF_GUIDE_ENABLED=true"
else
  for k in "${FLAG_KEYS[@]}"; do
    set_flag "$k" "true"
    info "set $k=true"
  done
fi

sha_after_edit="$(shasum -a 256 "$CONF_PATH" | awk '{print $1}')"
printf '%s\n' "$sha_after_edit" > "$STAMP_DIR/conf-sha256-after-edit.txt"

# reload
before_reload_lines="$(wc -l < logs/anthropic_proxy.log 2>/dev/null || echo 0)"
./manage.sh reload >> "$STAMP_DIR/reload.out" 2>&1 || die "reload 失败（见 $STAMP_DIR/reload.out）"
sleep 2
tail -n +$(( before_reload_lines + 1 )) logs/anthropic_proxy.log > "$STAMP_DIR/reload.log" 2>/dev/null || true
grep -q "\[RELOAD\] OK" "$STAMP_DIR/reload.log" || warn "未见 [RELOAD] OK 行（见 reload.log）"

# ------------------------------------------------------------
# 功能探针：3 次同路径 fnf → 日志应出现 fnf_guided=1
# ------------------------------------------------------------
probe_ts="$(wc -l < logs/anthropic_proxy.log 2>/dev/null || echo 0)"
NOW_S="$(date +%s)"
PROBE_OK=0
for i in 1 2 3; do
  t0=$(( NOW_S - 600 + i ))
  t1=$(( NOW_S - 590 + i ))
  cat > /tmp/fnf_probe_$$.json <<JSON
{"model":"claude-sonnet-4-6","max_tokens":32,"stream":false,
 "tools":[{"name":"Read","description":"read file","input_schema":{"type":"object","properties":{"file_path":{"type":"string"}},"required":["file_path"]}}],
 "messages":[
  {"role":"user","content":"probe"},
  {"role":"assistant","content":[{"type":"tool_use","id":"p$i","name":"Read","input":{"file_path":"/tmp/fnf-activation-probe/nonexistent-$i.py"}}]},
  {"role":"user","content":[{"type":"tool_result","tool_use_id":"p$i","content":"File does not exist: /tmp/fnf-activation-probe/nonexistent.py"}]},
  {"role":"assistant","content":[{"type":"tool_use","id":"q$i","name":"Read","input":{"file_path":"/tmp/fnf-activation-probe/nonexistent.py"}}]},
  {"role":"user","content":[{"type":"tool_result","tool_use_id":"q$i","content":"File does not exist: /tmp/fnf-activation-probe/nonexistent.py"}]},
  {"role":"assistant","content":[{"type":"tool_use","id":"r$i","name":"Read","input":{"file_path":"/tmp/fnf-activation-probe/nonexistent.py"}}]},
  {"role":"user","content":[{"type":"tool_result","tool_use_id":"r$i","content":"File does not exist: /tmp/fnf-activation-probe/nonexistent.py"}]}
 ]}
JSON
  curl -s --max-time 60 -X POST "$PROXY_URL/v1/messages" \
    -H 'Content-Type: application/json' \
    -H 'x-api-key: fnf-activation-probe' \
    -d @/tmp/fnf_probe_$$.json > /dev/null 2>&1 || true
done
rm -f /tmp/fnf_probe_$$.json
sleep 2
tail -n +$(( probe_ts + 1 )) logs/anthropic_proxy.log > "$STAMP_DIR/probe.log" 2>/dev/null || true
if grep -q "fnf_guided=" "$STAMP_DIR/probe.log"; then
  PROBE_OK=1
  info "功能探针通过：日志出现 fnf_guided=（旗标确已生效）"
  grep -a "Error translation" "$STAMP_DIR/probe.log" | tail -2 | sed 's/^/    /' | tee -a "$STAMP_DIR/probe-summary.txt"
else
  warn "功能探针未见 fnf_guided= —— 旗标可能未生效（查 $STAMP_DIR/probe.log）"
fi

# ------------------------------------------------------------
# 窗后快照 + 戳记
# ------------------------------------------------------------
curl -s --max-time 10 "$METRICS_URL" > "$STAMP_DIR/metrics-post.txt" 2>/dev/null || true
curl -s --max-time 10 "$PROXY_URL/api/status" > "$STAMP_DIR/status-post.json" 2>/dev/null || true
sha_after="$(shasum -a 256 "$CONF_PATH" | awk '{print $1}')"
printf '%s\n' "$sha_after" > "$STAMP_DIR/conf-sha256-after.txt"

cat > "$STAMP_DIR/STAMP.md" <<STAMP
# fnf 一期四旗标激活戳记

- 时间：$(date '+%Y-%m-%d %H:%M:%S')
- 激活配置：$CONF
- conf sha256（改前）：$sha_before
- conf sha256（改后）：$sha_after
- 代理 PID：$proxy_pid
- 旗标：$( [[ "$ONLY" == "guide" ]] && echo "F1 only" || echo "F1+F2+F3'+F3" )
- 功能探针（fnf_guided=）：$( [[ "$PROBE_OK" == "1" ]] && echo PASS || echo "未观测到（查 probe.log）" )
- 窗前/窗后快照：metrics-pre.txt / metrics-post.txt、status-pre.json / status-post.json
- reload 日志：reload.log（末次 [RELOAD] OK 生效参数）

判读方（swe-eval）请按方案 §4 预注册判据分段判读（按本戳记时刻分段）。
STAMP

echo
info "✅ 激活完成 —— 戳记: $STAMP_DIR/STAMP.md"
info "  conf sha256: $sha_before → $sha_after"
info "  功能探针: $( [[ "$PROBE_OK" == "1" ]] && echo PASS || echo "未见 fnf_guided（查 probe.log）" )"
exit 0
