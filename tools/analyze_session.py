#!/usr/bin/env python3
"""Session-level analyzer: timeline, routing switches, and performance.

Usage:
    python3 tools/analyze_session.py <session_id>
    python3 tools/analyze_session.py <session_id> --html   # output HTML page
    python3 tools/analyze_session.py <session_id> --open    # write to /tmp and print path

The session_id is the same short id shown on /status (e.g., 015a23a8).
"""
import os
import sys
import webbrowser

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from admin_server import _analyze_session, _build_session_html


def _text_summary(session_id):
    data = _analyze_session(session_id)
    if data["total"] == 0:
        print(f"未找到会话数据: {session_id}")
        return

    total = data["total"]
    dur = data["duration_seconds"]
    dur_str = f"{dur:.0f}s" if dur < 120 else f"{dur/60:.1f}min"

    print(f"=== Session {session_id} ===")
    print(f"时间范围: {data['start_ts']} -> {data['end_ts']} ({dur_str})")
    print(f"总请求数: {total}")
    print(f"路由分布: local={data['local_count']} cloud={data['cloud_count']} unknown={data['unknown_count']} errors={data['error_count']}")
    print(f"性能: avg={data['avg_duration_ms']:.0f}ms p95={data['p95_duration_ms']:.0f}ms p99={data['p99_duration_ms']:.0f}ms")
    print(f"上下文: peak_chars={data['max_input_chars']:,} total_out={data['total_output_chars']:,}")
    print(f"估算云成本: ¥{data['cloud_cost']:.4f}")

    if data["switches"]:
        print(f"\n路由切换 ({len(data['switches'])} 次):")
        for sw in data["switches"]:
            ts = sw["ts"][11:19] if len(sw["ts"]) >= 19 else sw["ts"]
            print(f"  #{sw['index']} {ts}: {sw['from']} -> {sw['to']} ({sw['reason']})")
    else:
        print("\n无路由切换")

    print("\n请求时间线 (前 20 条):")
    for r in data["timeline"][:20]:
        ts = r["ts"][11:19] if len(r["ts"]) >= 19 else r["ts"]
        dur_ms = r["duration_ms"]
        dur_str = f"{dur_ms/1000:.2f}s" if dur_ms >= 1000 else f"{dur_ms:.0f}ms"
        flags = []
        if r["fallback"]:
            flags.append("fallback")
        if r["emergency"]:
            flags.append("emergency")
        if r["blocker"]:
            flags.append("blocker")
        if r["loop_max_run"]:
            flags.append(f"loop({r['loop_max_run']})")
        print(f"  #{r['index']} {ts} target={r['target']:<12} chars={r['input_chars']:>7,} dur={dur_str:>7} status={r['status']} {' '.join(flags)}")


def _write_html(session_id):
    html = _build_session_html(session_id)
    out = os.path.join("/tmp", f"session_{session_id}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    session_id = sys.argv[1]
    if "--html" in sys.argv:
        print(_build_session_html(session_id))
    elif "--open" in sys.argv:
        out = _write_html(session_id)
        print(f"已生成: {out}")
        webbrowser.open(f"file://{out}")
    else:
        _text_summary(session_id)


if __name__ == "__main__":
    main()
