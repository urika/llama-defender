#!/usr/bin/env python3
"""config_fixture.py — 配置维度测试夹具 + 恢复哨兵（stdlib only）。

把"改全局配置→测试→恢复"的手工纪律(tearDown/finally)机制化:
  1. patch_config(**overrides)  上下文管理器: 改前快照, 退出精确恢复
  2. ConfigRestoreSentinel      套件级哨兵: 断言 proxy_state 关键属性
                                相对导入时快照零漂移(防跨测试串扰)

状态存储维度见 state_fixture.py; 本模块只管 CONFIG_REGISTRY 可见属性。
"""
import contextlib

# 注册表键 → proxy_state 属性名(与 _RELOAD_SPEC 同源关系, 此处独立维护
# 是因为要覆盖非 reloadable 但测试会改的属性, 如 TOOL_ALWAYS_KEEP)
_EXTRA_ATTRS = ("TOOL_ALWAYS_KEEP",)


def _snapshot():
    """全量快照 proxy_state 的注册表可见属性。返回 {attr: value}。"""
    import proxy_state as _ps
    import proxy_config
    snap = {}
    for key in proxy_config.CONFIG_REGISTRY:
        attr = key
        if hasattr(_ps, attr):
            snap[attr] = getattr(_ps, attr)
    for attr in _EXTRA_ATTRS:
        if hasattr(_ps, attr):
            snap[attr] = getattr(_ps, attr)
    return snap


@contextlib.contextmanager
def patch_config(**overrides):
    """临时改写 proxy_state 配置属性, 退出时精确恢复快照。

    用法:
        with patch_config(PROXY_TOOL_FILTER_MAX=3, PROXY_PD_ENABLED=False):
            ...  # 被测代码读到的是覆盖值
        # 退出后恢复原值——无论中途是否异常

    属性名必须在 proxy_state 上已存在(防止测试拼写错误静默造出新属性)。
    """
    import proxy_state as _ps
    unknown = [k for k in overrides if not hasattr(_ps, k)]
    if unknown:
        raise AttributeError(f"proxy_state 无这些属性(检查拼写/注册): {unknown}")
    saved = {k: getattr(_ps, k) for k in overrides}
    try:
        for k, v in overrides.items():
            setattr(_ps, k, v)
        yield saved
    finally:
        for k, v in saved.items():
            setattr(_ps, k, v)


class ConfigRestoreSentinel:
    """套件/文件级哨兵: 捕获快照, verify() 断言零漂移。

    用法(unittest):
        class MyCase(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                cls._sentinel = ConfigRestoreSentinel().capture()
            @classmethod
            def tearDownClass(cls):
                drifted = cls._sentinel.verify()
                assert not drifted, f"配置泄漏(未恢复): {drifted}"

    verify() 返回 {attr: (captured, current)}——仅含漂移项; 空字典 = 干净。
    """

    def __init__(self, attrs=None):
        # attrs: 限定监视野(默认全注册表+扩展), 缩窗可降开销
        self.attrs = attrs
        self.captured = None

    def capture(self):
        snap = _snapshot()
        if self.attrs:
            snap = {k: v for k, v in snap.items() if k in self.attrs}
        self.captured = snap
        return self

    def verify(self):
        if self.captured is None:
            raise RuntimeError("先调用 capture()")
        now = _snapshot()
        return {k: (v, now.get(k, "<deleted>"))
                for k, v in self.captured.items() if now.get(k, "<deleted>") != v}


__all__ = ["patch_config", "ConfigRestoreSentinel"]
