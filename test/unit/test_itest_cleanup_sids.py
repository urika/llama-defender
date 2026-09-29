#!/usr/bin/env python3
"""integration 用例会话 SID 一致性 + 哨兵覆盖面的静态检查（L-58 回归锚）。

出典（2026-09-29，L-58）：五个 integration 用例的 `diag_cleanup` 实参与其请求头
`x-claude-code-session-id` **错配**——

    test_blocker_integration.sh      头 itest-blocker      清理 itest-bl
    test_cache_align_integration.sh  头 itest-cache-align  清理 itest-ca
    test_compress_integration.sh     头 itest-compress     清理 itest-co
    test_long_context_integration.sh 头 itest-longctx      清理 itest-lo
    test_recall_hint_integration.sh  头 itesthint          清理 itesthin

⇒ trap 删的是不存在的文件、diag 残留 13 件（manifest/ledger/archive/orig 四区）
⇒ `state sentinel` 恒红，把后续真实回归掩盖在常红里（与 L-46 同族）。

本检查把这类错配挡在套件内：①每个用例用到的会话 SID 必须出现在其 diag_cleanup
实参里（`$SID` 形态按脚本内变量定义解析）；②哨兵的 diag 区面必须与 diag_cleanup
的清理面一致（L-58 前哨兵漏 `orig/`，该区泄漏长期不可见）。
"""
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INTEGRATION_DIR = os.path.join(ROOT, "test", "integration")
LIB_DIR = os.path.join(ROOT, "test", "lib")

HEADER_RE = re.compile(r"x-claude-code-session-id:\s*([^\"'\s\\]+)", re.I)
ASSIGN_RE = re.compile(r"^\s*(?:local\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)="
                       r"(?P<value>\"[^\"]*\"|'[^']*'|\S+)\s*$", re.M)
CLEANUP_RE = re.compile(r"diag_cleanup\s+([^\n]*)")
QUOTED_RE = re.compile(r"\"([^\"]*)\"|'([^']*)'")
VARIABLE_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
ARRAY_EXPAND_RE = re.compile(r"\$\{(\w+)\[@\]\}")
ARRAY_APPEND_RE = re.compile(r"(\w+)\+=\(([^)]*)\)")
ARRAY_SET_RE = re.compile(r"^\s*(\w+)=\(([^)]*)\)", re.M)
DIAG_DIR_RE = re.compile(r"logs/diag/([A-Za-z0-9_]+)/")
# 哨兵静态扫描的命名面（与 state_sentinel.sh 的 _SENTINEL_TEST_PREFIXES 一致）
SENTINEL_PREFIXES = ("itest", "pfshadow")


def _script_vars(text):
    """脚本内 `NAME="value"` 形式赋值 → {NAME: value}（含 local 前缀；数组除外）。"""
    variables = {}
    for match in ASSIGN_RE.finditer(text):
        value = match.group("value").strip("'\"")
        if value.startswith("("):          # `ARR=(...)` 是数组声明，非标量
            continue
        variables[match.group("name")] = value
    return variables


def _script_arrays(text):
    """数组字面量/追加（`ARR=(...)` 与 `ARR+=(...)`）→ {ARR: [元素…]}。"""
    arrays = {}
    for pattern in (ARRAY_SET_RE, ARRAY_APPEND_RE):
        for match in pattern.finditer(text):
            name = match.group(1)
            for quoted in QUOTED_RE.findall(match.group(2)):
                token = quoted[0] or quoted[1]
                if token:
                    arrays.setdefault(name, []).append(token)
    return arrays


def _resolve(token, variables):
    """把 `$SID` / `${SID}` / 字面量解析成具体值；解析不出返回 None。

    注：`${ARR[@]}` 是数组展开（由 `_cleanup_sids` 单独处理），这里返回 None；
    动态值（如 `itest-loop-$(date +%s)`）原样返回——两侧同一脚本内解析口径一致，
    因此"头 SID == 清理 SID"的比对对动态 SID 同样成立。
    """
    token = (token or "").strip()
    if not token or ARRAY_EXPAND_RE.search(token):
        return None
    if token.startswith("$"):
        match = VARIABLE_RE.match(token)
        return variables.get(match.group(1)) if match else None
    return token


def _header_sids(text, variables):
    sids = set()
    for raw in HEADER_RE.findall(text):
        value = _resolve(raw, variables)
        if value:
            sids.add(value)
    return sids


def _cleanup_sids(text, variables, arrays):
    sids = set()

    def _absorb(token):
        value = _resolve(token, variables)
        if not value or value.startswith("$") or "/" in value:
            return                          # 未解析的引用 / 路径类变量不算 SID
        sids.add(value)

    for match in CLEANUP_RE.finditer(text):
        args = match.group(1)
        for array_name in ARRAY_EXPAND_RE.findall(args):     # "${SIDS[@]}"
            for element in arrays.get(array_name, []):
                _absorb(element)
        for quoted in QUOTED_RE.findall(args):
            _absorb(quoted[0] or quoted[1])
        for bare in re.findall(r"(?<![\w$])\$[A-Za-z_][A-Za-z0-9_]*", args):
            _absorb(bare)
    return sids


class ItestCleanupSidTest(unittest.TestCase):
    def test_header_sids_are_cleaned(self):
        checked = 0
        for path in sorted(glob.glob(os.path.join(INTEGRATION_DIR, "*.sh"))):
            text = open(path, encoding="utf-8", errors="replace").read()
            if "diag_cleanup" not in text:
                continue
            variables = _script_vars(text)
            arrays = _script_arrays(text)
            headers = _header_sids(text, variables)
            if not headers:
                continue
            checked += 1
            cleaned = _cleanup_sids(text, variables, arrays)
            leaked = sorted(headers - cleaned)
            self.assertEqual(
                leaked, [],
                "%s：请求头 SID %s 未出现在 diag_cleanup 实参（%s）里"
                "——trap 会漏清该会话的 diag 状态（L-58 形态）"
                % (os.path.basename(path), leaked, sorted(cleaned)))
        self.assertGreater(checked, 5, "检查面异常：未扫到用例")

    def test_sid_prefix_is_visible_to_sentinel(self):
        """用例用的 SID 必须以 itest/pfshadow 开头——否则泄漏对哨兵不可见。

        出典（L-58）：test_loop_integration.sh 曾用 loop-test-/persist-test- 前缀，
        实测残留 8 件 archive/ledger 而哨兵（只扫 itest*/pfshadow*）毫无察觉。
        """
        offenders = []
        for path in sorted(glob.glob(os.path.join(INTEGRATION_DIR, "*.sh"))):
            text = open(path, encoding="utf-8", errors="replace").read()
            if "x-claude-code-session-id" not in text:
                continue
            for sid in _header_sids(text, _script_vars(text)):
                if not sid.startswith(SENTINEL_PREFIXES):
                    offenders.append("%s: %s" % (os.path.basename(path), sid))
        self.assertEqual(
            offenders, [],
            "以下会话 SID 不在哨兵扫描面内（itest*/pfshadow*）——泄漏不可见: %s"
            % offenders)


class SentinelCoverageTest(unittest.TestCase):
    """哨兵的 diag 区面 == diag_cleanup 的清理面（L-58：曾漏 orig/）。"""

    ARRAY_RE = re.compile(r"_SENTINEL_DIAG_DIRS=\(([^)]*)\)")

    def test_sentinel_dirs_match_cleanup_dirs(self):
        cleanup = open(os.path.join(LIB_DIR, "diag_cleanup.sh"),
                       encoding="utf-8").read()
        sentinel = open(os.path.join(LIB_DIR, "state_sentinel.sh"),
                        encoding="utf-8").read()
        cleaned_dirs = set(DIAG_DIR_RE.findall(cleanup))
        array = self.ARRAY_RE.search(sentinel)
        self.assertIsNotNone(array, "哨兵未声明 _SENTINEL_DIAG_DIRS")
        sentinel_dirs = set(array.group(1).split())
        self.assertTrue(cleaned_dirs, "diag_cleanup 未解析出目录")
        missing = sorted(cleaned_dirs - sentinel_dirs)
        self.assertEqual(
            missing, [],
            "哨兵未覆盖 diag_cleanup 会清的区: %s——该区泄漏不可见（L-58 形态）"
            % missing)


if __name__ == "__main__":
    unittest.main()
