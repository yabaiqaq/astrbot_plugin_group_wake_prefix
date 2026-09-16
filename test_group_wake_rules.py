"""离线单测：验证群唤醒前缀的核心判定与 CommandFilter monkeypatch 集成。

不依赖 AstrBot 运行时，直接对 group_wake_rules 进行验证。
运行：python test_group_wake_rules.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import group_wake_rules as wr  # noqa: E402


class Plain:
    """模拟 AstrBot 的 Plain 文本消息段。"""

    def __init__(self, text):
        self.text = text


class At:
    """模拟 AstrBot 的 At 消息段。"""

    def __init__(self, qq):
        self.qq = qq


class AtAll:
    """模拟 AstrBot 的 AtAll 消息段。"""


class Reply:
    """模拟 AstrBot 的 Reply 消息段。"""

    def __init__(self, sender_id):
        self.sender_id = sender_id


class MockEvent:
    def __init__(self, group_id, message_str, is_wake=False, messages=None, self_id="10001"):
        self._group_id = group_id
        self.message_str = message_str
        self.is_at_or_wake_command = is_wake
        self._extras = {}
        self._messages = messages if messages is not None else []
        self._self_id = self_id

    def get_message_str(self):
        return self.message_str

    def get_messages(self):
        return self._messages

    def get_group_id(self):
        return self._group_id

    def get_self_id(self):
        return self._self_id

    def set_extra(self, k, v):
        self._extras[k] = v

    def get_extra(self, k=None, default=None):
        if k is None:
            return self._extras
        return self._extras.get(k, default)


class FakeCommandFilter:
    """模拟 AstrBot 的命令过滤器：is_at_or_wake_command + 命令名开头匹配。"""

    def __init__(self, name):
        self.command_name = name

    def filter(self, event, cfg):
        text = event.message_str.strip()
        if not event.is_at_or_wake_command:
            return False
        if text == self.command_name or text.startswith(self.command_name + " "):
            return True
        return False


class FakePlugin:
    def __init__(self, cache, suppress=True, match_mode="prefix"):
        self._cache = cache
        self.config = {"suppress_builtin": suppress, "match_mode": match_mode}

    def _get_prefixes(self, gid, event=None):
        return self._cache.get(gid, [])

    def _is_mgmt_command(self, text):
        return wr.is_mgmt_command(text)


def run(name, fn):
    fn()
    print(f"PASS  {name}")


def test_custom_prefix_triggers_command():
    plugin = FakePlugin({"123": ["$"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)
    # 自定义前缀不在内置 wake_prefix 中，WakingCheckStage 不会唤醒 -> is_wake=False
    event = MockEvent("123", "$点歌", is_wake=False)
    cmd = FakeCommandFilter("点歌")
    result = cmd.filter(event, None)
    assert result is True, f"自定义前缀应激活命令, got {result}"
    assert event.message_str == "点歌", event.message_str
    assert event.is_at_or_wake_command is True
    assert event.get_extra("_gwake_stripped") is True


def test_suppress_builtin():
    plugin = FakePlugin({"123": ["$"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)
    # 内置 / 是 wake_prefix，WakingCheckStage 会设 is_wake=True 并剥离 / -> "点歌"
    event = MockEvent("123", "点歌", is_wake=True)
    cmd = FakeCommandFilter("点歌")
    result = cmd.filter(event, None)
    assert result is False, "内置唤醒应被屏蔽"
    assert event.is_at_or_wake_command is False


def test_mgmt_always_allowed():
    plugin = FakePlugin({"123": ["$"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)
    # /delwake 经内置唤醒后 message="delwake"
    event = MockEvent("123", "delwake", is_wake=True)
    cmd = FakeCommandFilter("delwake")
    result = cmd.filter(event, None)
    assert result is True, "管理指令应始终放行"


def test_custom_prefix_mgmt():
    plugin = FakePlugin({"123": ["$"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)
    event = MockEvent("123", "$delwake", is_wake=False)
    cmd = FakeCommandFilter("delwake")
    result = cmd.filter(event, None)
    assert result is True
    assert event.message_str == "delwake"


def test_no_prefix_normal():
    plugin = FakePlugin({})  # 该群无前缀
    wr.install_patch(FakeCommandFilter, lambda: plugin)
    event = MockEvent("123", "help", is_wake=True)
    cmd = FakeCommandFilter("help")
    result = cmd.filter(event, None)
    assert result is True
    assert event.message_str == "help"


def test_only_prefix():
    plugin = FakePlugin({"123": ["$"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)
    event = MockEvent("123", "$", is_wake=False)
    cmd = FakeCommandFilter("点歌")
    result = cmd.filter(event, None)
    assert result is False
    assert event.is_at_or_wake_command is False


def test_plain_no_wake():
    plugin = FakePlugin({"123": ["$"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)
    event = MockEvent("123", "你好机器人", is_wake=False)
    cmd = FakeCommandFilter("点歌")
    result = cmd.filter(event, None)
    assert result is False
    assert event.is_at_or_wake_command is False  # 未被错误修改


def test_contains_mode_wakes():
    plugin = FakePlugin({"123": ["顾问"]}, match_mode="contains")
    wr.install_patch(FakeCommandFilter, lambda: plugin)
    event = MockEvent("123", "顾问 点歌", is_wake=False)
    cmd = FakeCommandFilter("点歌")
    result = cmd.filter(event, None)
    # contains 模式仅唤醒（不剥离），命令能否匹配取决于原逻辑
    assert event.is_at_or_wake_command is True
    # 因前缀不在开头，原逻辑下命令名不匹配则不激活
    assert result is False


def test_suppress_off_keeps_builtin():
    plugin = FakePlugin({"123": ["$"]}, suppress=False)
    wr.install_patch(FakeCommandFilter, lambda: plugin)
    event = MockEvent("123", "点歌", is_wake=True)  # 内置唤醒
    cmd = FakeCommandFilter("点歌")
    result = cmd.filter(event, None)
    assert result is True, "关闭屏蔽时应保留内置唤醒"
    assert event.is_at_or_wake_command is True


def make_fresh_filter_cls():
    """新建一个干净的命令过滤器类。

    注意：前面的用例已经给 FakeCommandFilter 打过补丁，若用子类会继承被污染的版本，
    因此这里必须造一个完全独立的新类。
    """

    class FreshCommandFilter:
        def __init__(self, name):
            self.command_name = name

        def filter(self, event, cfg):
            if not event.is_at_or_wake_command:
                return False
            text = event.message_str.strip()
            return text == self.command_name or text.startswith(self.command_name + " ")

    return FreshCommandFilter


def test_reinstall_uses_latest_instance():
    """同一模块内重复安装补丁时，必须采用最新实例（旧实例不得残留生效）。"""
    cls = make_fresh_filter_cls()
    plugin_a = FakePlugin({"123": ["$"]})  # 旧实例：有前缀
    wr.install_patch(cls, lambda: plugin_a)
    plugin_b = FakePlugin({})  # 新实例：前缀已清空（模拟 /delwake）
    wr.install_patch(cls, lambda: plugin_b)

    event = MockEvent("123", "help", is_wake=True)
    assert cls("help").filter(event, None) is True, "重复安装后应恢复内置唤醒"
    assert event.is_at_or_wake_command is True


def test_reload_no_stacking_and_restore():
    """模拟 AstrBot 重载插件：sys.modules 被清理 -> 模块全局重置 -> 再次安装补丁。

    这是 v1.2.0 的回归场景：旧实现会把旧实例的补丁层当成「原始 filter」包进去，
    导致 /delwake 之后旧层仍在屏蔽，系统默认唤醒永远无法恢复。
    """
    import importlib

    cls = make_fresh_filter_cls()
    plugin_a = FakePlugin({"123": ["$"]})  # 重载前：有前缀
    wr.install_patch(cls, lambda: plugin_a)

    # 模拟重载：清掉 sys.modules 中的模块并重新导入（模块级全局变量被重置）
    for k in list(sys.modules):
        if k.startswith("group_wake_rules"):
            del sys.modules[k]
    wr2 = importlib.import_module("group_wake_rules")

    plugin_b = FakePlugin({})  # 重载后：执行了 /delwake，缓存已清空
    wr2.install_patch(cls, lambda: plugin_b)

    # 关键断言：旧实例 A 的前缀不得再生效，内置唤醒必须恢复
    event = MockEvent("123", "help", is_wake=True)
    assert cls("help").filter(event, None) is True, "重载并清除前缀后，内置唤醒必须恢复"
    assert event.is_at_or_wake_command is True

    # 新实例设置前缀后，屏蔽仍需正常工作（说明新层确实生效）
    plugin_b._cache["123"] = ["$"]
    event2 = MockEvent("123", "help", is_wake=True)
    assert cls("help").filter(event2, None) is False, "新实例设置前缀后应继续屏蔽内置唤醒"


def test_reload_v121_over_v120_unwraps():
    """v1.2.1 叠在 v1.2.0 之上：必须正确剥离旧层、取回真原始 filter。

    关键 bug：v1.2.0 的 patched_filter 闭包里有多个 callable（original/plugin_getter），
    按字母顺序遍历会先取到 plugin_getter 而非 original，导致剥离失败、旧层继续屏蔽。
    v1.2.1 的 install_patch 必须按「惯例命名（original）」取真正的被包裹函数。
    """
    import importlib

    cls = make_fresh_filter_cls()
    true_orig = cls.filter  # 真原始 filter（模块名不会匹配插件前缀）

    # ---- 复刻 v1.2.0 的补丁：在工厂函数内构造，使 original 成为闭包单元 ----
    _v120_plugin = FakePlugin({"123": ["$"]})

    def make_v120_patched(original):
        plugin_getter = lambda: _v120_plugin  # noqa: E731

        def patched(self, event, cfg):
            plugin = plugin_getter()
            try:
                gid = event.get_group_id()
            except Exception:
                gid = ""
            prefixes = plugin._cache.get(gid, []) if gid else []
            if prefixes:
                event.is_at_or_wake_command = False
                return False
            return original(self, event, cfg)

        patched.__module__ = "astrbot_plugin_group_wake_prefix.group_wake_rules"
        return patched

    cls.filter = make_v120_patched(true_orig)

    # ---- 现在模拟重载 + 重新安装 v1.2.1 ----
    for k in list(sys.modules):
        if k.startswith("group_wake_rules"):
            del sys.modules[k]
    wr2 = importlib.import_module("group_wake_rules")

    plugin_b = FakePlugin({})  # 新实例 /delwake 已清空
    wr2.install_patch(cls, lambda: plugin_b)

    # 关键断言：v1.2.0 旧层必须被剥离；否则旧实例前缀会继续屏蔽
    event = MockEvent("123", "help", is_wake=True)
    assert cls("help").filter(event, None) is True, (
        "v1.2.1 叠在 v1.2.0 之上时，旧层未剥离会导致 /delwake 后无法恢复"
    )
    assert event.is_at_or_wake_command is True

    # 新实例重新设前缀，屏蔽要正常生效（说明新层确实生效）
    plugin_b._cache["123"] = ["$"]
    event2 = MockEvent("123", "help", is_wake=True)
    assert cls("help").filter(event2, None) is False


def test_prefix_equals_wake_prefix_not_suppressed():
    """自定义前缀恰好等于某个 wake_prefix 时，不得被屏蔽（否则机器人彻底不回话）。

    模拟 WakingCheckStage 已把 wake_prefix「#」从 message_str 上剥掉：
    message_str 变成「点歌 周杰伦」，但消息链里仍是原始的「#点歌 周杰伦」。
    修复前只看 message_str，会误判为「未命中自定义前缀 + 已被内置唤醒」而被屏蔽。
    """
    plugin = FakePlugin({"123": ["#"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)

    ev = MockEvent(
        "123",
        "点歌 周杰伦",  # WakingCheckStage 剥离后的 message_str
        is_wake=True,
        messages=[Plain("#点歌 周杰伦")],  # 用户原始输入
    )
    assert FakeCommandFilter("点歌").filter(ev, None) is True, (
        "自定义前缀 == wake_prefix 时，命令必须能触发"
    )
    assert ev.is_at_or_wake_command is True
    # 已被剥离过，不应重复剥离
    assert ev.message_str == "点歌 周杰伦"


def test_prefix_equals_wake_prefix_still_suppresses_others():
    """同一配置下，未带自定义前缀、也无社交信号的普通唤醒消息仍应被屏蔽。"""
    plugin = FakePlugin({"123": ["#"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)

    # 无 At/Reply 段、由 / 或 wake_prefix 唤醒的普通文本（原始输入里没有自定义前缀）
    ev = MockEvent("123", "你好", is_wake=True, messages=[Plain("你好")])
    assert FakeCommandFilter("help").filter(ev, None) is False
    assert ev.is_at_or_wake_command is False


def test_at_mention_not_suppressed():
    """@ 提及本机器人：不屏蔽，保留唤醒（对话场景）。"""
    plugin = FakePlugin({"123": ["#"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)

    ev = MockEvent(
        "123",
        "你好机器人",
        is_wake=True,
        messages=[At("10001"), Plain("你好机器人")],
    )
    assert FakeCommandFilter("help").filter(ev, None) is False  # 非命令文本，不激活命令
    assert ev.is_at_or_wake_command is True  # 但唤醒被保留，可进入 LLM 对话


def test_at_mention_with_command_not_suppressed():
    """@ 提及 + 命令文本：不屏蔽，与原生日志一致（@ 后跟指令可正常触发）。"""
    plugin = FakePlugin({"123": ["#"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)

    ev = MockEvent(
        "123",
        "help",
        is_wake=True,
        messages=[At("10001"), Plain("help")],
    )
    assert FakeCommandFilter("help").filter(ev, None) is True
    assert ev.is_at_or_wake_command is True


def test_reply_not_suppressed():
    """引用回复本机器人：不屏蔽，保留唤醒。"""
    plugin = FakePlugin({"123": ["#"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)

    ev = MockEvent(
        "123",
        "再说一遍",
        is_wake=True,
        messages=[Reply("10001"), Plain("再说一遍")],
    )
    assert FakeCommandFilter("help").filter(ev, None) is False
    assert ev.is_at_or_wake_command is True


def test_at_all_not_suppressed():
    """@全体：不屏蔽，保留唤醒。"""
    plugin = FakePlugin({"123": ["#"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)

    ev = MockEvent(
        "123",
        "大家好",
        is_wake=True,
        messages=[AtAll(), Plain("大家好")],
    )
    assert FakeCommandFilter("help").filter(ev, None) is False
    assert ev.is_at_or_wake_command is True


def test_at_others_still_suppressed():
    """@ 的是别人（非本机器人）：不视为社交唤醒，仍按前缀唤醒屏蔽。"""
    plugin = FakePlugin({"123": ["#"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)

    ev = MockEvent(
        "123",
        "你好",
        is_wake=True,
        messages=[At("99999"), Plain("你好")],
    )
    assert FakeCommandFilter("help").filter(ev, None) is False
    assert ev.is_at_or_wake_command is False


def test_reply_others_still_suppressed():
    """引用的是别人发的消息：不视为社交唤醒，仍按前缀唤醒屏蔽。"""
    plugin = FakePlugin({"123": ["#"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)

    ev = MockEvent(
        "123",
        "你好",
        is_wake=True,
        messages=[Reply("99999"), Plain("你好")],
    )
    assert FakeCommandFilter("help").filter(ev, None) is False
    assert ev.is_at_or_wake_command is False


def test_raw_text_fallback_without_messages():
    """消息链为空时，退化到 message_str（保持旧行为）。"""
    plugin = FakePlugin({"123": ["$"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)

    ev = MockEvent("123", "$点歌 周杰伦", is_wake=False)
    assert FakeCommandFilter("点歌").filter(ev, None) is True
    assert ev.message_str == "点歌 周杰伦"


def test_raw_text_used_when_messages_available():
    """消息链可用且含原始前缀时，按原始文本匹配、按当前 message_str 剥离。"""
    plugin = FakePlugin({"123": ["$"]})
    wr.install_patch(FakeCommandFilter, lambda: plugin)

    ev = MockEvent("123", "$点歌 周杰伦", is_wake=False, messages=[Plain("$点歌 周杰伦")])
    assert FakeCommandFilter("点歌").filter(ev, None) is True
    assert ev.message_str == "点歌 周杰伦"


def test_merge_prefixes_accumulates():
    """/setwake 新增模式：多次设置累积，不覆盖之前的前缀。"""
    assert wr.merge_prefixes([], ["a"]) == ["a"]
    assert wr.merge_prefixes(["a"], ["b"]) == ["a", "b"]
    assert wr.merge_prefixes(["a", "b"], ["c", "d"]) == ["a", "b", "c", "d"]


def test_merge_prefixes_dedup():
    """重复设置相同前缀自动去重，空白前缀忽略。"""
    assert wr.merge_prefixes(["a"], ["a"]) == ["a"]
    assert wr.merge_prefixes(["a"], ["b", "a"]) == ["a", "b"]
    assert wr.merge_prefixes(["a"], ["", "  "]) == ["a"]
    assert wr.merge_prefixes([" a "], ["a"]) == ["a"]


def test_remove_prefixes_batch():
    """/delwake 带参批量移除：只删指定前缀，保留其余。"""
    assert wr.remove_prefixes(["a", "b", "c"], ["b"]) == ["a", "c"]
    assert wr.remove_prefixes(["a", "b", "c"], ["a", "c"]) == ["b"]
    assert wr.remove_prefixes(["a", "b"], ["x"]) == ["a", "b"]  # 不存在项忽略
    assert wr.remove_prefixes(["a", "b"], []) == ["a", "b"]  # 空移除不改变
    assert wr.remove_prefixes(["a", "b"], [" b "]) == ["a"]  # strip 后匹配
    assert wr.remove_prefixes(["a"], ["a", "a"]) == []  # 重复移除安全


def test_load_prefixes_from_file_disk_truth():
    """磁盘 JSON 是前缀的唯一真相来源（模拟「旧实例内存缓存过期」场景）。

    模拟：实例 A 在内存里记着本群有前缀，但磁盘已被 /delwake 写空。
    补丁若只读内存就会继续屏蔽；改为读磁盘则应立即返回空 -> 恢复默认唤醒。
    """
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "group_prefixes.json"
        # 模拟 /delwake 已把磁盘写成空
        p.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")

        # 即便「旧实例」内存里还残留本群前缀，磁盘读取必须是空
        stale_memory = {"123": ["#"]}
        assert wr.load_prefixes_from_file(str(p), "123") == [], (
            "磁盘为空时，load_prefixes_from_file 必须返回 []，"
            "否则旧实例内存残留会继续屏蔽默认唤醒"
        )

        # 模拟 /setwake # 写入磁盘
        p.write_text(
            json.dumps({"123": ["#"]}, ensure_ascii=False), encoding="utf-8"
        )
        assert wr.load_prefixes_from_file(str(p), "123") == ["#"], (
            "磁盘已写入前缀时，load_prefixes_from_file 必须返回最新值"
        )

        # 其它群不受影响
        assert wr.load_prefixes_from_file(str(p), "999") == []

        # 文件不存在时返回 []
        missing = Path(td) / "nope.json"
        assert wr.load_prefixes_from_file(str(missing), "123") == []


def test_hot_reload_strips_foreign_legacy_layer():
    """热重载残留旧补丁层（仍攥着已删除前缀）必须被新安装剥掉。

    复现用户现场：旧层（v1.2.3 风格）闭包捕获一个『内存含 #』的旧实例，
    继续屏蔽 /；新 handler (wakestatus) 读盘显示已空 -> 造成分裂假象。
    v1.2.5 的 install_patch 必须先把 CommandFilter.filter 强制重置回原始，
    再装一层指向当前实例的补丁，旧层因此被彻底剥掉。
    """
    import json
    import tempfile
    from pathlib import Path

    cls = make_fresh_filter_cls()
    true_orig = cls.filter

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "group_prefixes.json"
        p.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")

        # —— 伪装一层「历史残留旧补丁」：闭包捕获内存含 # 的旧实例 ——
        legacy_plugin = FakePlugin({"123": ["#"]})  # 内存里还攥着 #

        def legacy_patched(self, event, cfg):
            plugin = legacy_plugin
            try:
                gid = event.get_group_id()
            except Exception:  # noqa: BLE001
                gid = ""
            prefixes = plugin._get_prefixes(gid) if gid else []
            if prefixes and wr.apply_wake_rules(
                prefixes, True, plugin._is_mgmt_command, event, "prefix"
            ):
                return False
            return true_orig(self, event, cfg)

        legacy_patched._gwake_patch = True
        legacy_patched.__module__ = "astrbot_plugin_group_wake_prefix.group_wake_rules"
        cls.filter = legacy_patched

        # 复现前提：旧层生效时 /help 应被屏蔽（即「/ 没恢复」）
        ev_before = MockEvent("123", "help", is_wake=True)
        assert cls("help").filter(ev_before, None) is False, "旧层残留时应屏蔽 /（复现前提）"

        # —— 现在执行 v1.2.5 的 install_patch（指向读盘的新实例）——
        new_plugin = FakePlugin({})

        def disk_get(gid, event=None):
            return wr.load_prefixes_from_file(str(p), gid)

        new_plugin._get_prefixes = disk_get
        wr.install_patch(cls, lambda: new_plugin)

        # 关键断言 1：补丁层数必须为 1（旧层已被剥掉）
        assert wr.count_patch_layers(cls) == 1, (
            f"热重载后补丁层数应为 1，实际 {wr.count_patch_layers(cls)}（旧层残留）"
        )
        # 关键断言 2：/help 现在放行（默认唤醒恢复）
        ev_after = MockEvent("123", "help", is_wake=True)
        assert cls("help").filter(ev_after, None) is True, (
            "热重载装新层后，/ 必须恢复（旧层残留未剥掉）"
        )
        assert ev_after.is_at_or_wake_command is True
        # 关键断言 3：拦截层生效实例是 new_plugin
        assert getattr(cls, "_gwake_active_plugin_id", None) == id(new_plugin)


if __name__ == "__main__":
    run("自定义前缀触发命令", test_custom_prefix_triggers_command)
    run("屏蔽内置唤醒", test_suppress_builtin)
    run("管理指令始终放行", test_mgmt_always_allowed)
    run("自定义前缀+管理指令", test_custom_prefix_mgmt)
    run("无前缀群正常", test_no_prefix_normal)
    run("仅前缀无内容", test_only_prefix)
    run("普通消息不误屏蔽", test_plain_no_wake)
    run("contains 模式唤醒", test_contains_mode_wakes)
    run("关闭屏蔽保留内置", test_suppress_off_keeps_builtin)
    run("重复安装采用最新实例", test_reinstall_uses_latest_instance)
    run("重载后不叠加且可恢复", test_reload_no_stacking_and_restore)
    run("v1.2.1 叠在 v1.2.0 之上可剥离", test_reload_v121_over_v120_unwraps)
    run("前缀==wake_prefix 不误屏蔽", test_prefix_equals_wake_prefix_not_suppressed)
    run(
        "前缀==wake_prefix 仍屏蔽其它",
        test_prefix_equals_wake_prefix_still_suppresses_others,
    )
    run("@提及不屏蔽", test_at_mention_not_suppressed)
    run("@提及+命令不屏蔽", test_at_mention_with_command_not_suppressed)
    run("引用回复不屏蔽", test_reply_not_suppressed)
    run("@全体不屏蔽", test_at_all_not_suppressed)
    run("@别人仍屏蔽", test_at_others_still_suppressed)
    run("引用别人仍屏蔽", test_reply_others_still_suppressed)
    run("无消息链时退化到 message_str", test_raw_text_fallback_without_messages)
    run("有消息链时按原始文本匹配", test_raw_text_used_when_messages_available)
    run("merge 新增累积", test_merge_prefixes_accumulates)
    run("merge 去重忽略空白", test_merge_prefixes_dedup)
    run("remove 批量移除", test_remove_prefixes_batch)
    run("磁盘 JSON 是前缀唯一真相", test_load_prefixes_from_file_disk_truth)
    run("热重载残留旧层必须被剥掉", test_hot_reload_strips_foreign_legacy_layer)
    print("\n全部测试通过 ✅")
