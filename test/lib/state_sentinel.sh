#!/usr/bin/env bash
# ============================================================
# state_sentinel.sh — 状态泄漏哨兵(元测试)。
#
# 套件前后各调一次, 捕获"测试框架不得污染生产 diag 状态"的违例:
#   begin: 记录 sessions.jsonl 行数基线 + diag 目录快照
#   end:   ①确定性: 测试会话存储文件必须已被清理(trap 失效即 fail)
#                 覆盖 itest*(集成测试) + pfshadow*(promptfoo 影子代理)
#          ②宽松:   sessions.jsonl 窗口内非测试增量 → WARN
#                   (实验批跑等生产流量会合法增长, 无法与测试泄漏区分;
#                    严格模式待生产流量静默窗口再启用)
#
# 用法(run_tests.sh 各 tier 内):
#   source "$SCRIPT_DIR/lib/state_sentinel.sh"
#   state_sentinel_begin
#   ... 测试 ...
#   state_sentinel_end   # 违例时非零返回
# ============================================================

_SENTINEL_BASELINE_FILE=""

state_sentinel_begin() {
    _SENTINEL_BASELINE_FILE="$(mktemp)"
    local sess="logs/diag/sessions.jsonl"
    if [[ -f "$sess" ]]; then
        wc -l < "$sess" | tr -d ' ' > "$_SENTINEL_BASELINE_FILE"
    else
        echo 0 > "$_SENTINEL_BASELINE_FILE"
    fi
}

state_sentinel_end() {
    local repo_root="${REPO_ROOT:-.}"
    local rc=0

    # ① 确定性: 测试会话存储残留(trap 清理失效的直接证据)
    #    覆盖: itest*(集成测试) + pfshadow*(promptfoo 影子代理, 2026-09-06)
    local leftovers
    leftovers=$(ls logs/diag/manifest/itest* logs/diag/index/itest* \
                   logs/diag/archive/itest* logs/diag/ledger/itest* \
                   logs/diag/manifest/pfshadow* logs/diag/index/pfshadow* \
                   logs/diag/archive/pfshadow* logs/diag/ledger/pfshadow* \
                   2>/dev/null || true)
    if [[ -n "$leftovers" ]]; then
        echo "  [SENTINEL][FAIL] 测试残留会话状态 itest*/pfshadow*(清理 trap 未生效):"
        echo "$leftovers" | sed 's/^/      /'
        rc=1
    fi

    # ② 宽松: sessions.jsonl 窗口增量的非测试行(生产流量同样会命中 → WARN)
    local sess="logs/diag/sessions.jsonl"
    if [[ -f "$sess" && -f "$_SENTINEL_BASELINE_FILE" ]]; then
        local before after new_lines non_itest
        before=$(cat "$_SENTINEL_BASELINE_FILE")
        after=$(wc -l < "$sess" | tr -d ' ')
        new_lines=$(( after - before ))
        if (( new_lines > 0 )); then
            non_itest=$(tail -n "$new_lines" "$sess" | grep -cvE "itest|pfshadow" || true)
            if (( non_itest > 0 )); then
                echo "  [SENTINEL][WARN] sessions.jsonl 窗口增量含 $non_itest 条非 itest 行"
                echo "      (可能为生产流量; 与测试泄漏不可区分, 不计失败)"
            fi
        fi
    fi
    [[ -n "$_SENTINEL_BASELINE_FILE" ]] && rm -f "$_SENTINEL_BASELINE_FILE"
    return $rc
}
