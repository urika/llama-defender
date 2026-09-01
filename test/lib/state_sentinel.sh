#!/usr/bin/env bash
# ============================================================
# state_sentinel.sh — 状态泄漏哨兵(元测试)。
#
# 集成套件前后各调一次, 捕获"测试框架不得污染生产 diag 状态"的违例:
#   begin: 记录 sessions.jsonl 行数基线 + diag 目录快照
#   end:   ①确定性: itest* 会话存储文件必须已被清理(trap 失效即 fail)
#          ②宽松:   sessions.jsonl 窗口内非 itest 增量 → WARN
#                   (实验批跑等生产流量会合法增长, 无法与测试泄漏区分;
#                    严格模式待生产流量静默窗口再启用)
#
# 用法(run_tests.sh run_integration 内):
#   source "$SCRIPT_DIR/lib/state_sentinel.sh"
#   state_sentinel_begin
#   ... 各集成测试 ...
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

    # ① 确定性: itest* 会话存储残留(trap 清理失效的直接证据)
    local leftovers
    leftovers=$(ls logs/diag/manifest/itest* logs/diag/index/itest* \
                   logs/diag/archive/itest* logs/diag/ledger/itest* \
                   2>/dev/null || true)
    if [[ -n "$leftovers" ]]; then
        echo "  [SENTINEL][FAIL] 集成测试残留 itest 会话状态(清理 trap 未生效):"
        echo "$leftovers" | sed 's/^/      /'
        rc=1
    fi

    # ② 宽松: sessions.jsonl 窗口增量的非 itest 行(生产流量同样会命中 → WARN)
    local sess="logs/diag/sessions.jsonl"
    if [[ -f "$sess" && -f "$_SENTINEL_BASELINE_FILE" ]]; then
        local before after new_lines non_itest
        before=$(cat "$_SENTINEL_BASELINE_FILE")
        after=$(wc -l < "$sess" | tr -d ' ')
        new_lines=$(( after - before ))
        if (( new_lines > 0 )); then
            non_itest=$(tail -n "$new_lines" "$sess" | grep -cv "itest" || true)
            if (( non_itest > 0 )); then
                echo "  [SENTINEL][WARN] sessions.jsonl 窗口增量含 $non_itest 条非 itest 行"
                echo "      (可能为生产流量; 与测试泄漏不可区分, 不计失败)"
            fi
        fi
    fi
    [[ -n "$_SENTINEL_BASELINE_FILE" ]] && rm -f "$_SENTINEL_BASELINE_FILE"
    return $rc
}
