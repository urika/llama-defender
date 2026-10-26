"""L-28 sessions.jsonl 按日归档保留策略单测（2026-10-26）。

覆盖: 日切触发（写入时检测活文件 mtime 日期变更 → sessions-YYYYMMDD.jsonl）、
10MB 溢出段 `.1` 随日切折叠、同日归档名冲突追加合并、过期归档清理
（PROXY_DIAG_SESSIONS_RETENTION_DAYS，边界=保留期当日保留）、0=无限保留、
24h 进程内节流、写入路径 fail-open（日切/清理失败不影响落盘不抛异常）。
设计决议: 同目录按日后缀（diag/archive/ 是 session_ledger 会话档案的家,
listdir 计 cap, 不混入）。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import proxy_state as _ps
import diagnostics as diag


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class SessionsRetentionTest(unittest.TestCase):
    """log_session_diag 日切 + _sessions_retention_cleanup 行为契约。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="l28_retention_")
        self._saved_path = _ps._DIAG_SESSIONS_PATH
        self._saved_days = _ps.PROXY_DIAG_SESSIONS_RETENTION_DAYS
        self._saved_throttle = dict(diag._retention_state)
        _ps._DIAG_SESSIONS_PATH = os.path.join(self._tmp, "sessions.jsonl")
        # 默认挡住写入路径旁路的清理检查(节流"刚查过"), 各清理用例自行复位
        diag._retention_state["last_check"] = time.monotonic()

    def tearDown(self):
        _ps._DIAG_SESSIONS_PATH = self._saved_path
        _ps.PROXY_DIAG_SESSIONS_RETENTION_DAYS = self._saved_days
        diag._retention_state.clear()
        diag._retention_state.update(self._saved_throttle)

    # ------------------------------------------------------------------
    # 日切
    # ------------------------------------------------------------------

    def test_same_day_write_keeps_live_file_only(self):
        """当日写入: 活文件追加, 不产生归档（兼容现状）。"""
        diag.log_session_diag({"session_key": "s1", "n": 1})
        diag.log_session_diag({"session_key": "s1", "n": 2})
        self.assertEqual(len(_read_jsonl(_ps._DIAG_SESSIONS_PATH)), 2)
        self.assertEqual([f for f in os.listdir(self._tmp)
                          if f.startswith("sessions-")], [])

    def test_day_cut_moves_previous_content_to_archive(self):
        """跨日写入: 旧内容按其写入日期转存 sessions-YYYYMMDD.jsonl。"""
        diag.log_session_diag({"session_key": "s1", "n": 1})
        past = time.time() - 2 * 86400
        past_day = datetime.fromtimestamp(past).strftime("%Y%m%d")
        os.utime(_ps._DIAG_SESSIONS_PATH, (past, past))  # 回拨 mtime 模拟昨日
        diag.log_session_diag({"session_key": "s1", "n": 2})
        archive = os.path.join(self._tmp, "sessions-%s.jsonl" % past_day)
        self.assertTrue(os.path.exists(archive))
        self.assertEqual(_read_jsonl(archive), [{"session_key": "s1", "n": 1}])
        # 活文件只含今日记录
        self.assertEqual(_read_jsonl(_ps._DIAG_SESSIONS_PATH),
                         [{"session_key": "s1", "n": 2}])

    def test_day_cut_folds_10mb_overflow_segment(self):
        """`.1`（10MB 同日溢出段）随日切一并并入归档且删除。"""
        with open(_ps._DIAG_SESSIONS_PATH + ".1", "w", encoding="utf-8") as f:
            f.write(json.dumps({"session_key": "s1", "n": 0}) + "\n")
        diag.log_session_diag({"session_key": "s1", "n": 1})
        past = time.time() - 2 * 86400
        past_day = datetime.fromtimestamp(past).strftime("%Y%m%d")
        os.utime(_ps._DIAG_SESSIONS_PATH, (past, past))
        diag.log_session_diag({"session_key": "s1", "n": 2})
        archive = os.path.join(self._tmp, "sessions-%s.jsonl" % past_day)
        self.assertEqual(_read_jsonl(archive),
                         [{"session_key": "s1", "n": 0}, {"session_key": "s1", "n": 1}])
        self.assertFalse(os.path.exists(_ps._DIAG_SESSIONS_PATH + ".1"))
        # 活文件日切后重建, 只含今日记录
        self.assertEqual(_read_jsonl(_ps._DIAG_SESSIONS_PATH),
                         [{"session_key": "s1", "n": 2}])

    def test_day_cut_merges_into_existing_archive(self):
        """归档名冲突（同日多段）→ 追加合并, 不覆盖不丢数据。"""
        past = time.time() - 2 * 86400
        past_day = datetime.fromtimestamp(past).strftime("%Y%m%d")
        archive = os.path.join(self._tmp, "sessions-%s.jsonl" % past_day)
        with open(archive, "w", encoding="utf-8") as f:
            f.write(json.dumps({"session_key": "earlier"}) + "\n")
        diag.log_session_diag({"session_key": "s1", "n": 1})
        os.utime(_ps._DIAG_SESSIONS_PATH, (past, past))
        diag.log_session_diag({"session_key": "s1", "n": 2})
        self.assertEqual(_read_jsonl(archive),
                         [{"session_key": "earlier"}, {"session_key": "s1", "n": 1}])
        self.assertEqual(_read_jsonl(_ps._DIAG_SESSIONS_PATH),
                         [{"session_key": "s1", "n": 2}])

    def test_day_cut_ignores_non_date_shaped_files(self):
        """非 `sessions-YYYYMMDD.jsonl` 形态的手工备份不受日切/清理影响。"""
        keeper = os.path.join(self._tmp, "sessions.jsonl.bak-prefilter")
        with open(keeper, "w", encoding="utf-8") as f:
            f.write("keep me\n")
        diag.log_session_diag({"session_key": "s1"})
        self.assertTrue(os.path.exists(keeper))

    # ------------------------------------------------------------------
    # 过期清理
    # ------------------------------------------------------------------

    def _reset_throttle(self):
        diag._retention_state["last_check"] = (
            time.monotonic() - diag._RETENTION_CHECK_INTERVAL_S)

    def test_expired_archives_deleted_boundary_kept(self):
        """过期归档删除; 恰好等于保留期边界（today-days）的保留（≥N 天语义）。"""
        _ps.PROXY_DIAG_SESSIONS_RETENTION_DAYS = 7
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
        before_cutoff = (datetime.now() - timedelta(days=8)).strftime("%Y%m%d")
        for name in ("sessions-19990101.jsonl",
                     "sessions-%s.jsonl" % before_cutoff,
                     "sessions-%s.jsonl" % cutoff,
                     "sessions-%s.jsonl" % datetime.now().strftime("%Y%m%d")):
            with open(os.path.join(self._tmp, name), "w", encoding="utf-8") as f:
                f.write("{}\n")
        self._reset_throttle()
        diag._sessions_retention_cleanup()
        remaining = sorted(os.listdir(self._tmp))
        self.assertIn("sessions-%s.jsonl" % cutoff, remaining)
        self.assertIn("sessions-%s.jsonl" % datetime.now().strftime("%Y%m%d"),
                      remaining)
        self.assertNotIn("sessions-19990101.jsonl", remaining)
        self.assertNotIn("sessions-%s.jsonl" % before_cutoff, remaining)

    def test_retention_zero_keeps_everything(self):
        """0 = 无限保留: 过期文件不删。"""
        _ps.PROXY_DIAG_SESSIONS_RETENTION_DAYS = 0
        expired = os.path.join(self._tmp, "sessions-19990101.jsonl")
        with open(expired, "w", encoding="utf-8") as f:
            f.write("{}\n")
        self._reset_throttle()
        diag._sessions_retention_cleanup()
        self.assertTrue(os.path.exists(expired))

    def test_cleanup_throttled_to_once_per_interval(self):
        """进程内节流: interval 内第二次调用不再扫描。"""
        _ps.PROXY_DIAG_SESSIONS_RETENTION_DAYS = 7
        self._reset_throttle()
        with mock.patch("os.listdir", return_value=[]) as ml:
            diag._sessions_retention_cleanup()
            self.assertEqual(ml.call_count, 1)
            diag._sessions_retention_cleanup()  # 节流窗口内 → 不扫描
            self.assertEqual(ml.call_count, 1)

    def test_cleanup_reads_config_dynamically(self):
        """保留天数热读 _ps（reloadable 生效路径）: 调大即救回本应过期的文件。"""
        expired = os.path.join(self._tmp, "sessions-19990101.jsonl")
        with open(expired, "w", encoding="utf-8") as f:
            f.write("{}\n")
        _ps.PROXY_DIAG_SESSIONS_RETENTION_DAYS = 0  # 置 0（无限）→ 不删
        self._reset_throttle()
        diag._sessions_retention_cleanup()
        self.assertTrue(os.path.exists(expired))

    # ------------------------------------------------------------------
    # fail-open
    # ------------------------------------------------------------------

    def test_write_path_fail_open_on_bad_dir(self):
        """路径不可用（父目录是文件）→ 日切/清理/落盘全链路不抛。"""
        blocker = os.path.join(self._tmp, "not_a_dir")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x\n")
        _ps._DIAG_SESSIONS_PATH = os.path.join(blocker, "sessions.jsonl")
        _ps.PROXY_DIAG_SESSIONS_RETENTION_DAYS = 7
        self._reset_throttle()
        diag.log_session_diag({"session_key": "s1"})  # 不抛即通过

    def test_cleanup_fail_open_on_missing_dir(self):
        """归档目录不存在（listdir OSError）→ 不抛。"""
        _ps._DIAG_SESSIONS_PATH = os.path.join(self._tmp, "no", "such", "dir",
                                               "sessions.jsonl")
        _ps.PROXY_DIAG_SESSIONS_RETENTION_DAYS = 7
        self._reset_throttle()
        diag._sessions_retention_cleanup()  # 不抛即通过


if __name__ == "__main__":
    unittest.main()
