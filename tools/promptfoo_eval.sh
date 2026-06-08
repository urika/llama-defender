#!/usr/bin/env bash
# ============================================================
# Promptfoo A/B 评估包装器
# 替代 run_experiment.sh 的 report 阶段 + 新增固定 prompt 回归测试
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
EXPERIMENT_DIR="$PROJECT_DIR/logs/experiments"
PROMPTFOO_BIN="$PROJECT_DIR/node_modules/.bin/promptfoo"
mkdir -p "$EXPERIMENT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# 检查依赖
_check_deps() {
    if [[ ! -x "$PROMPTFOO_BIN" ]]; then
        error "Promptfoo 未安装，请先运行: cd $PROJECT_DIR && npm install promptfoo @libsql/darwin-arm64"
        exit 1
    fi
    if ! curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4000/v1/models 2>/dev/null | grep -q '^200$'; then
        error "代理未运行 (http://127.0.0.1:4000)，请先启动代理"
        exit 1
    fi
}

# 运行单组评估
cmd_eval() {
    local group=""
    local desc=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --group|-g) group="$2"; shift 2 ;;
            --desc|-d)  desc="$2";  shift 2 ;;
            *) error "未知参数: $1"; exit 1 ;;
        esac
    done
    if [[ -z "$group" ]]; then
        error "用法: $0 eval --group A|B [--desc '描述']"
        exit 1
    fi

    _check_deps

    local ts; ts=$(date +%Y%m%d-%H%M%S)
    local output_prefix="$EXPERIMENT_DIR/promptfoo-${group}-${ts}"
    local description="${desc:-Promptfoo A/B ${group}组 $(date +%Y-%m-%d\ %H:%M:%S)}"

    info "启动 Promptfoo 评估: 组=$group"
    info "描述: $description"
    info "代理状态: $(curl -s http://127.0.0.1:4000/v1/models 2>/dev/null | head -c 80)"

    # 运行 promptfoo eval
    # --no-cache: 确保每次运行都是新鲜结果
    # --no-share: 不上传到云端（隐私）
    # --output: 同时输出 JSON 和 HTML
    cd "$PROJECT_DIR"
    "$PROMPTFOO_BIN" eval \
        --config promptfooconfig.yaml \
        --description "$description" \
        --tag "group=${group}" \
        --tag "ts=${ts}" \
        --output "${output_prefix}.json" \
        --output "${output_prefix}.html" \
        --no-cache \
        --no-share \
        --table \
        2>&1 | tee "${output_prefix}.log"

    local exit_code=${PIPESTATUS[0]}
    if [[ $exit_code -ne 0 ]]; then
        error "Promptfoo 评估失败 (exit=$exit_code)"
        exit $exit_code
    fi

    info "✅ ${group} 组评估完成"
    info "  JSON 结果: ${output_prefix}.json"
    info "  HTML 报告: ${output_prefix}.html"
    info "  运行日志: ${output_prefix}.log"

    # 合并代理日志指标（如果存在）
    local proxy_log=""
    # 查找最近的同组代理日志
    proxy_log=$(ls -t "$EXPERIMENT_DIR"/${group}-*.log 2>/dev/null | head -1)
    if [[ -n "$proxy_log" && -f "$proxy_log" ]]; then
        local merged_report="${output_prefix}_merged.md"
        info "合并代理日志指标: $proxy_log"
        python3 "$SCRIPT_DIR/promptfoo_report_merge.py" \
            --promptfoo "${output_prefix}.json" \
            --log "$proxy_log" \
            --output "$merged_report" \
            2>/dev/null || warn "合并报告失败，跳过"
        if [[ -f "$merged_report" ]]; then
            info "  统一报告: $merged_report"
        fi
    fi

    # 生成摘要
    echo ""
    echo "=== 评估摘要 ==="
    if command -v python3 &>/dev/null; then
        python3 -c "
import json, sys
try:
    with open('${output_prefix}.json') as f:
        data = json.load(f)
    results = data.get('results', {}).get('results', [])
    total = len(results)
    passed = sum(1 for r in results if r.get('success', False))
    print(f'  总测试数: {total}')
    print(f'  通过: {passed}')
    print(f'  失败: {total - passed}')
    print(f'  通过率: {passed/total*100:.1f}%')
    # 显示各断言统计
    assert_types = {}
    for r in results:
        for a in r.get('gradingResult', {}).get('componentResults', []):
            t = a.get('assertion', {}).get('type', 'unknown')
            p = a.get('pass', False)
            if t not in assert_types:
                assert_types[t] = {'pass': 0, 'fail': 0}
            assert_types[t]['pass' if p else 'fail'] += 1
    if assert_types:
        print('  断言统计:')
        for t, s in assert_types.items():
            print(f'    {t}: {s[\"pass\"]} pass / {s[\"fail\"]} fail')
except Exception as e:
    print(f'  解析结果时出错: {e}')
"
    fi
}

# 对比 A/B 两组最新结果
cmd_compare() {
    local a_json b_json
    a_json=$(ls -t "$EXPERIMENT_DIR"/promptfoo-A-*.json 2>/dev/null | head -1)
    b_json=$(ls -t "$EXPERIMENT_DIR"/promptfoo-B-*.json 2>/dev/null | head -1)

    if [[ -z "$a_json" ]]; then
        error "未找到 A 组 Promptfoo 结果"; exit 1
    fi
    if [[ -z "$b_json" ]]; then
        error "未找到 B 组 Promptfoo 结果"; exit 1
    fi

    info "对比 A/B 两组 Promptfoo 结果..."
    info "  A 组: $a_json"
    info "  B 组: $b_json"

    local report_file="$EXPERIMENT_DIR/promptfoo_ab_report_$(date +%Y%m%d-%H%M%S).md"

    python3 "$SCRIPT_DIR/promptfoo_report_merge.py" \
        --promptfoo "$a_json" \
        --output "$report_file" \
        2>/dev/null || {
        # fallback: inline Python via heredoc (avoids shell quoting issues)
        python3 << PYEOF
import json, sys
from datetime import datetime

a_path = "$a_json"
b_path = "$b_json"
report_path = "$report_file"

with open(a_path) as f: a = json.load(f)
with open(b_path) as f: b = json.load(f)

a_results = a.get('results', {}).get('results', [])
b_results = b.get('results', {}).get('results', [])

with open(report_path, 'w') as out:
    out.write('# Promptfoo A/B 对比报告\n\n')
    out.write('生成时间: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + '\n\n')
    out.write('## A 组\n\n')
    out.write('```json\n' + json.dumps(a, indent=2, ensure_ascii=False)[:500] + '\n```\n\n')
    out.write('## B 组\n\n')
    out.write('```json\n' + json.dumps(b, indent=2, ensure_ascii=False)[:500] + '\n```\n\n')
    out.write('## 对比摘要\n\n')
    out.write('| 指标 | A 组 | B 组 |\n')
    out.write('|------|------|------|\n')
    a_pass = sum(1 for r in a_results if r.get('success'))
    b_pass = sum(1 for r in b_results if r.get('success'))
    out.write(f'| 总测试数 | {len(a_results)} | {len(b_results)} |\n')
    out.write(f'| 通过 | {a_pass} | {b_pass} |\n')
    out.write(f'| 失败 | {len(a_results)-a_pass} | {len(b_results)-b_pass} |\n')
    out.write(f'| 通过率 | {a_pass/len(a_results)*100:.1f}% | {b_pass/len(b_results)*100:.1f}% |\n')
    out.write('\n## 原始结果文件\n\n')
    out.write(f'- A 组: `{a_path}`\n')
    out.write(f'- B 组: `{b_path}`\n')

print(f'报告已生成: {report_path}')
PYEOF
        info "✅ 报告已生成: $report_file"
    }
}

# 查看最近的 HTML 报告
cmd_view() {
    local latest_html
    latest_html=$(ls -t "$EXPERIMENT_DIR"/promptfoo-*.html 2>/dev/null | head -1)
    if [[ -z "$latest_html" ]]; then
        error "未找到 Promptfoo HTML 报告"
        exit 1
    fi
    info "打开报告: $latest_html"
    open "$latest_html" 2>/dev/null || xdg-open "$latest_html" 2>/dev/null || echo "请在浏览器中打开: $latest_html"
}

# 启动 Promptfoo Web UI 查看历史结果
cmd_ui() {
    _check_deps
    info "启动 Promptfoo Web UI..."
    cd "$PROJECT_DIR"
    "$PROMPTFOO_BIN" view --no
}

# 帮助
cmd_help() {
    cat << 'EOF'
Promptfoo A/B 评估包装器

用法:
  ./tools/promptfoo_eval.sh eval --group A|B [--desc '描述']
  ./tools/promptfoo_eval.sh compare
  ./tools/promptfoo_eval.sh view
  ./tools/promptfoo_eval.sh ui
  ./tools/promptfoo_eval.sh help

命令说明:
  eval      对指定组运行 Promptfoo 固定 prompt 回归测试
  compare   对比最近的 A/B 两组 Promptfoo 结果
  view      在浏览器中打开最新的 HTML 报告
  ui        启动 Promptfoo Web UI 查看所有历史结果
  help      显示此帮助

示例:
  # A 组评估（代理已以 clearing 开启模式启动）
  ./tools/promptfoo_eval.sh eval --group A --desc 'clearing 开启'

  # B 组评估（代理已以 clearing 关闭模式启动）
  ./tools/promptfoo_eval.sh eval --group B --desc 'clearing 关闭'

  # 对比两组结果
  ./tools/promptfoo_eval.sh compare

环境要求:
  - 代理已启动 (http://127.0.0.1:4000)
  - Promptfoo 已安装 (npm install promptfoo @libsql/darwin-arm64)

EOF
}

main() {
    case "${1:-help}" in
        eval)    shift; cmd_eval "$@" ;;
        compare) cmd_compare ;;
        view)    cmd_view ;;
        ui)      cmd_ui ;;
        help|--help|-h) cmd_help ;;
        *) error "未知命令: $1"; echo "使用 '$0 help' 查看用法"; exit 1 ;;
    esac
}

main "$@"
