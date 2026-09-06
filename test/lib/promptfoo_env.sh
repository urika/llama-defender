#!/usr/bin/env bash
# ============================================================
# promptfoo_env.sh — promptfoo 影子代理测试环境（2026-09-06）。
#
# 背景（EXP-2 批后 promptfoo 门禁误报确诊）：promptfoo 直打生产代理存在
# 三重环境缺陷——①测的不是待提交代码（生产进程代码滞后，reload 不载码）；
# ②共享生产 ctx_engine canonical（promptfoo 单发独立 prompt 共会话键,
# engine append-only 把多题追加成连发, contains 断言假阴/假阳）；③测试
# 流量污染生产 diag/台账。
#
# 本环境照搬集成测试先例：从工作树启动影子代理（测的就是待提交代码），
# 独立端口 + 受控 env + 独立日志目录，共用真实后端（promptfoo 断言的是
# 模型输出内容，mock 无意义）。引擎在影子环境强制关闭——promptfoo 的
# 职责是「模型+管线基线」回归，engine 行为自有 unit/组装场景/ctx-case
# 三层专属覆盖。
#
# 用法（在 run_tests.sh 的 run_promptfoo 内）:
#   source "$REPO_ROOT/test/lib/promptfoo_env.sh"
#   pf_shadow_start || { skip...; return; }
#   ... promptfoo eval --config "$PF_SHADOW_CONFIG" ...
#   pf_shadow_stop
# ============================================================

PF_PROXY_PORT="${PF_PROXY_PORT:-4021}"
PF_LOG_DIR="${PF_LOG_DIR:-}"
PF_PROXY_PID=""

pf_shadow_stop() {
  set +e
  if [[ -n "$PF_PROXY_PID" ]] && kill -0 "$PF_PROXY_PID" 2>/dev/null; then
    kill "$PF_PROXY_PID" 2>/dev/null
    sleep 0.2
    kill -9 "$PF_PROXY_PID" 2>/dev/null
  fi
  PF_PROXY_PID=""
  # 影子会话的 diag 足迹清理。diag_cleanup.sh 按集成脚本惯例由使用方
  # source——run_promptfoo 作用域没有它, 这里无条件补 source(幂等);
  # 首次启用时该守卫曾静默跳过清理, 被状态哨兵抓获(2026-09-06)。
  if [[ -n "${REPO_ROOT:-}" && -f "$REPO_ROOT/test/lib/diag_cleanup.sh" ]]; then
    # shellcheck source=/dev/null
    source "$REPO_ROOT/test/lib/diag_cleanup.sh"
    diag_cleanup "$REPO_ROOT" "pfshadow"
  fi
}

pf_shadow_start() {
  # 前置: 真实后端可达（模型内容断言的依赖; 不可达让调用方 skip）
  local backend="${LLAMA_BASE_URL:-http://127.0.0.1:8081/v1}"
  if ! curl -sf --max-time 3 "${backend%/v1}/v1/models" >/dev/null 2>&1 \
     && ! curl -sf --max-time 3 "${backend%/v1}/models" >/dev/null 2>&1; then
    echo "  [pf-shadow] backend not reachable at ${backend%/v1} — cannot start"
    return 1
  fi
  PF_LOG_DIR="$REPO_ROOT/logs/itest_pf"
  mkdir -p "$PF_LOG_DIR"
  : > "$PF_LOG_DIR/proxy.log"

  # 复刻 manage.sh 启动语义: 先 source 激活配置 + 密钥（模型别名解析、
  # catalog、云回退 key 都依赖它——集成测试用 mock 后端可以不带, promptfoo
  # 打真实后端必须带）, 再用下方显式 env 覆盖隔离关键项（引擎/去重/旗标）。
  # shellcheck source=/dev/null
  set -a
  # shellcheck disable=SC1091
  source "$REPO_ROOT/configs/active.conf" 2>/dev/null
  # shellcheck disable=SC1091
  [[ -f "$REPO_ROOT/configs/secret.local.conf" ]] && \
    source "$REPO_ROOT/configs/secret.local.conf" 2>/dev/null
  set +a

  # 影子版 promptfoo 配置: 复制仓库配置, 替换端点端口 + 注入独立会话头
  # （pfshadow 键供 teardown 的 diag_cleanup 精确清扫; 引擎已关, 会话键
  #  不参与 canonical 累积）
  PF_SHADOW_CONFIG="$PF_LOG_DIR/promptfooconfig.shadow.yaml"
  sed "s#apiBaseUrl: 'http://127.0.0.1:4000'#apiBaseUrl: 'http://127.0.0.1:$PF_PROXY_PORT'#" \
      "$REPO_ROOT/promptfooconfig.yaml" > "$PF_SHADOW_CONFIG"
  if ! grep -q "127.0.0.1:$PF_PROXY_PORT" "$PF_SHADOW_CONFIG"; then
    echo "  [pf-shadow] failed to generate shadow config (endpoint pattern miss)"
    return 1
  fi

  PORT="$PF_PROXY_PORT" \
  LLAMA_BASE_URL="$backend" \
  PROXY_LOG_PATH="$PF_LOG_DIR/proxy.log" \
  PROXY_METRICS_ENABLED=false \
  PROXY_DIAG_ENABLED=true \
  PROXY_HBE_ENABLED=false \
  PROXY_QUEUE_ENABLED=false \
  PROXY_DEDUP_WINDOW=0 \
  PROXY_ROUTE_ENABLED=false \
  PROXY_COMPRESS_ENABLED=false \
  PROXY_CLEAR_ENABLED=false \
  PROXY_PD_ENABLED=true \
  PROXY_PD_MICRO_TURN_ENABLED=false \
  PROXY_CTX_ENGINE_ENABLED=false \
  PROXY_AUTO_RECALL_ENABLED=false \
  PROXY_TOMBSTONE_RECALL_ENABLED=false \
  PROXY_CTX_LIMIT_ENABLED=false \
    python3 "$REPO_ROOT/anthropic_proxy.py" >>"$PF_LOG_DIR/proxy.log" 2>&1 &
  PF_PROXY_PID=$!

  # 会话头注入（若当前 promptfoo 版本支持 provider headers）: 把影子流量
  # 的会话键钉为 pfshadow, teardown 清理可精确命中; 不支持则静默回退
  # 客户端派生键（cli_*）, 只影响清扫精度不影响正确性
  sed -i '' "s#^      max_tokens: 1024#      max_tokens: 1024\\
      headers:\\
        'x-claude-code-session-id': 'pfshadow'#" "$PF_SHADOW_CONFIG" 2>/dev/null \
    || sed -i "s#^      max_tokens: 1024#      max_tokens: 1024\\n      headers:\\n        'x-claude-code-session-id': 'pfshadow'#" "$PF_SHADOW_CONFIG"

  for _ in $(seq 1 50); do
    if curl -sf --max-time 1 "http://127.0.0.1:$PF_PROXY_PORT/v1/models" >/dev/null 2>&1; then
      echo "  [pf-shadow] proxy up on :$PF_PROXY_PORT (engine=off, fresh code, isolated state)"
      return 0
    fi
    sleep 0.1
  done
  echo "  [pf-shadow] proxy failed to start on :$PF_PROXY_PORT"
  tail -20 "$PF_LOG_DIR/proxy.log" 2>/dev/null
  pf_shadow_stop
  return 1
}
