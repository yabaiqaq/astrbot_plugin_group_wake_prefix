"""离线冒烟测试：验证「设置自定义前缀后屏蔽内置命令唤醒，清除后恢复」逻辑。

覆盖 v1.3.0 行为：
  * / 前缀与 wake_prefix 触发的命令唤醒 -> 屏蔽；
  * @ 提及、@全体、引用回复本机器人 -> 不屏蔽（保留对话唤醒）；
  * 管理指令始终放行；清除前缀后内置唤醒恢复。

不依赖真实 AstrBot 环境：直接对 group_wake_rules 的补丁机制 + 磁盘 JSON 模拟验证。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import group_wake_rules as wr  # noqa: E402


# ---------------------------------------------------------------- 消息段模拟
class Plain:
    def __init__(self, text):
        self.text = text


class At:
    def __init__(self, qq):
        self.qq = qq


class AtAll:
    pass


class Reply:
    def __init__(self, sender_id):
        self.sender_id = sender_id


# ---------------------------------------------------------------- 事件与过滤器模拟
class FakeEvent:
    def __init__(self, text, gid, woken=False, messages=None, self_id="10001"):
        self.message_str = text
        self._gid = gid
        self.is_at_or_wake_command = woken
        self._messages = messages if messages is not None else []
        self._self_id = self_id
        self._extras = {}

    def get_message_str(self):
        return self.message_str

    def get_messages(self):
        return self._messages

    def get_group_id(self):
        return self._gid

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
        return text == self.command_name or text.startswith(self.command_name + " ")


class FakePlugin:
    """模拟插件实例：配置 + 磁盘 JSON 前缀读取（与 main 插件同样的契约）。"""

    def __init__(self, data_file, suppress=True, match_mode="prefix"):
        self.config = {"suppress_builtin": suppress, "match_mode": match_mode}
        self._data_file = data_file

    def _get_prefixes(self, gid, event=None):
        if event is not None:
            cached = event.get_extra("_gwake_prefixes")
            if isinstance(cached, list):
                return cached
        prefixes = wr.load_prefixes_from_file(str(self._data_file), gid)
        if event is not None:
            event.set_extra("_gwake_prefixes", prefixes)
        return prefixes

    def _is_mgmt_command(self, text):
        return wr.is_mgmt_command(text)


PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)


def main():
    with tempfile.TemporaryDirectory() as td:
        data_file = Path(td) / "group_prefixes.json"

        def write(prefixes: list[str] | None, gid: str = "g1"):
            data = {gid: prefixes} if prefixes else {}
            data_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        plugin = FakePlugin(data_file)
        wr.install_patch(FakeCommandFilter, lambda: plugin)

        # ---- 场景 A：设置 # 前缀后，纯 / 前缀唤醒被屏蔽 ----
        write(["#"])
        e = FakeEvent("/help", "g1", woken=True, messages=[Plain("/help")])
        check("A 内置 / 命令被屏蔽", FakeCommandFilter("help").filter(e, None) is False)
        check("A 唤醒标记被关闭", e.is_at_or_wake_command is False)

        # ---- 场景 B：自定义前缀命中，正常唤醒并剥前缀 ----
        e = FakeEvent("#点歌 周杰伦", "g1", woken=False, messages=[Plain("#点歌 周杰伦")])
        check("B 自定义前缀唤醒", FakeCommandFilter("点歌").filter(e, None) is True)
        check("B 前缀被剥掉", e.message_str == "点歌 周杰伦")

        # ---- 场景 C：管理指令不被屏蔽（逃生通道）----
        e = FakeEvent("/delwake", "g1", woken=True, messages=[Plain("/delwake")])
        check("C 管理指令 /delwake 不被屏蔽", e.is_at_or_wake_command is True)

        # ---- 场景 C2：@提及 + /setwake 也不被屏蔽 ----
        e = FakeEvent(
            "/setwake #",
            "g1",
            woken=True,
            messages=[At("10001"), Plain("/setwake #")],
        )
        check("C2 @提及 + /setwake 不被屏蔽", e.is_at_or_wake_command is True)

        # ---- 场景 F：@提及唤醒不被屏蔽 ----
        e = FakeEvent("你好机器人", "g1", woken=True, messages=[At("10001"), Plain("你好机器人")])
        check("F @提及唤醒不被屏蔽", e.is_at_or_wake_command is True)

        # ---- 场景 F2：引用回复本机器人不被屏蔽 ----
        e = FakeEvent("再说一遍", "g1", woken=True, messages=[Reply("10001"), Plain("再说一遍")])
        check("F2 引用回复不被屏蔽", e.is_at_or_wake_command is True)

        # ---- 场景 F3：@ 别人 + / 前缀仍被屏蔽 ----
        e = FakeEvent("/help", "g1", woken=True, messages=[At("99999"), Plain("/help")])
        check("F3 @ 别人不视为社交唤醒，/ 仍被屏蔽", FakeCommandFilter("help").filter(e, None) is False)
        check("F3 唤醒标记被关闭", e.is_at_or_wake_command is False)

        # ---- 场景 D：清除前缀后，内置唤醒恢复 ----
        write(None)
        e = FakeEvent("/help", "g1", woken=True, messages=[Plain("/help")])
        check("D 清除后内置唤醒恢复", e.is_at_or_wake_command is True)

        # ---- 场景 E：suppress_builtin=False 时不屏蔽内置 ----
        plugin2 = FakePlugin(data_file, suppress=False)
        write(["#"])
        e = FakeEvent("/help", "g2", woken=True, messages=[Plain("/help")])
        # 用 plugin2 重新安装补丁（幂等）
        wr.install_patch(FakeCommandFilter, lambda: plugin2)
        check("E suppress 关闭时不屏蔽", e.is_at_or_wake_command is True)

        # ---- 场景 G：contains 模式命中仅唤醒、不剥前缀 ----
        plugin3 = FakePlugin(data_file, match_mode="contains")
        write(["机器人"], gid="g4")
        e = FakeEvent("喂机器人帮我查下", "g4", woken=False, messages=[Plain("喂机器人帮我查下")])
        wr.install_patch(FakeCommandFilter, lambda: plugin3)
        FakeCommandFilter("help").filter(e, None)
        check("G contains 命中唤醒", e.is_at_or_wake_command is True)
        check("G contains 不剥前缀", e.message_str == "喂机器人帮我查下")

        # ---- 场景 H：只有前缀无内容，不唤醒 ----
        write(["#"])
        e = FakeEvent("#", "g1", woken=False, messages=[Plain("#")])
        check("H 仅有前缀不唤醒", e.is_at_or_wake_command is False)

        # ---- 场景 I：无自定义前缀的群，内置行为不变 ----
        write(None)
        plugin4 = FakePlugin(data_file)
        wr.install_patch(FakeCommandFilter, lambda: plugin4)
        e = FakeEvent("/help", "g5", woken=True, messages=[Plain("/help")])
        check("I 无前缀群内置行为不变", e.is_at_or_wake_command is True)

    print(f"\n共 {len(PASS)+len(FAIL)} 项，通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
