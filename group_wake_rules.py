"""群唤醒前缀核心判定逻辑（与 AstrBot 运行时解耦，便于离线单测）。

本模块解决 AstrBot 的两个架构限制（基于 master 源码确认）：

1. 命令（command handler）的激活完全由 ``WakingCheckStage`` 阶段、依据**那一刻**
   的 ``event.is_at_or_wake_command`` 决定，而插件自身的 handler 全在该阶段之后
   执行，届时再去修改 ``is_at_or_wake_command`` / ``message_str`` 已无法影响命令激活。
   因此「自定义前缀触发命令」必须在命令过滤器被调用前改写 event 状态。

2. ``WakingCheckStage`` 之后没有任何插件可插入的 pipeline stage 能改回已激活的
   handler 列表（star_request 用的是局部副本），所以「屏蔽内置唤醒」也只能从命令
   过滤器这一统一入口下手。

做法：monkeypatch ``CommandFilter.filter``（所有命令激活的唯一入口，在
WakingCheckStage 内部对每个命令都被调用），在本群有自定义前缀时：
  * 命中自定义前缀 -> 剥离前缀、置唤醒标志，让后续命令按命令名匹配（触发命令）；
  * 未命中但被系统内置机制（/ 前缀、平台 wake_prefix）唤醒 -> 屏蔽之，
    仅本群自定义前缀可唤醒命令；
  * @ 提及、@全体、引用回复本机器人（消息段含 At/AtAll/Reply）-> **不屏蔽**，
    保留对话唤醒能力，与原生行为一致；
  * 本插件管理指令始终放行，保证可随时 /delwake 恢复。
"""

import json
from pathlib import Path

MGMT_COMMANDS = {"setwake", "delwake", "wakeprefix", "wakestatus"}


def load_prefixes_from_file(path, gid: str) -> list:
    """从持久化 JSON 读取某群的前缀（群级别真相来源）。

    与内存缓存解耦：即使插件被重载、实例被替换，只要磁盘 JSON 已更新，
    这里读到的就是最新值——这正是 /delwake 之后系统默认唤醒能恢复的前提。
    文件不存在 / 损坏 / 格式不对时返回空列表，绝不抛异常。
    """
    try:
        if not path:
            return []
        p = Path(path)
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        return data.get(gid, []) or []
    except Exception:  # noqa: BLE001
        return []


def is_mgmt_command(text: str) -> bool:
    """判断一条消息是否以本插件管理指令开头（兼容带不带 / 前缀）。"""
    t = (text or "").strip()
    if t.startswith("/"):
        t = t[1:].strip()
    first = t.split(None, 1)[0].lower() if t else ""
    return first in MGMT_COMMANDS


def raw_message_text(event) -> str:
    """取用户「原始输入」文本，即**尚未**被 WakingCheckStage 剥离 wake_prefix 的版本。

    必要性（基于 master 源码 ``waking_check/stage.py`` 第 112-129 行）：
    WakingCheckStage 命中 wake_prefix 后会执行
    ``event.message_str = event.message_str[len(wake_prefix):].strip()``，
    把前缀从 ``message_str`` 上剥掉。因此当本群自定义前缀**恰好等于**某个
    wake_prefix（例如都是 ``#``）时，等到 CommandFilter 被调用，
    ``message_str`` 已经变成「点歌」——自定义前缀永远匹配不上，
    消息会直接落进「未命中但已被内置唤醒」分支而被屏蔽，机器人彻底不回话。

    消息链里的 ``Plain`` 消息段保留着原始文本，据此重建可免疫上述剥离。
    取不到时退化到 ``event.get_message_str()``，保证向后兼容。
    """
    try:
        comps = event.get_messages()
    except Exception:  # noqa: BLE001
        comps = None
    if comps:
        buf = []
        for c in comps:
            if type(c).__name__ != "Plain":
                continue
            t = getattr(c, "text", None)
            if isinstance(t, str):
                buf.append(t)
        raw = "".join(buf).strip()
        if raw:
            return raw
    try:
        return (event.get_message_str() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def is_at_or_reply_wake(event) -> bool:
    """判断本次唤醒是否来自「@ 提及 / @全体 / 引用回复本机器人」。

    对应 AstrBot ``WakingCheckStage`` 中 wake_prefix 之外的唤醒分支
    （``astrbot/core/pipeline/waking_check/stage.py``）：

    * 消息段含 ``At`` 且 qq == 本机器人 id（被 @ 提及）；
    * 消息段含 ``AtAll``（若 AstrBot 配置 ``ignore_at_all=True``，
      该消息本就不会被唤醒，也不会走到这里）；
    * 消息段含 ``Reply`` 且 sender_id == 本机器人 id（引用了机器人发的消息）。

    返回 ``True`` 表示消息带有「社交性唤醒」信号：屏蔽内置唤醒时应放行，
    仅靠 / 前缀与 wake_prefix 触发的命令唤醒才被屏蔽。
    读取消息段失败时保守返回 ``False``（保持原有屏蔽行为，不误放行）。
    """
    try:
        messages = event.get_messages()
        self_id = str(event.get_self_id())
    except Exception:  # noqa: BLE001
        return False
    if not messages:
        return False
    for m in messages:
        try:
            tn = type(m).__name__
            if tn == "At":
                if str(getattr(m, "qq", "")) == self_id:
                    return True
            elif tn == "AtAll":
                return True
            elif tn == "Reply":
                if str(getattr(m, "sender_id", "")) == self_id:
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def apply_wake_rules(
    prefixes: list[str],
    suppress_builtin: bool,
    is_mgmt,
    event,
    match_mode: str = "prefix",
) -> bool:
    """在命令过滤器（CommandFilter.filter）被调用前改写 event 状态。

    返回 ``True`` 表示应当屏蔽（调用方应直接 ``return False``，不激活该命令）；
    返回 ``False`` 表示继续走原始命令匹配逻辑。

    匹配依据为**原始输入** ``raw``（见 :func:`raw_message_text`），
    剥离则作用在当前的 ``message_str`` 上，且只剥仍实际存在的前缀。

    副作用（命中自定义前缀时）：
      * prefix 模式：``event.message_str`` 被剥离前缀、``is_at_or_wake_command`` 置 True，
        并打 ``_gwake_stripped`` 标记（避免重复剥离 / 误屏蔽其它命令）；
      * contains 模式：仅置 ``is_at_or_wake_command=True`` 唤醒（前缀不在开头，剥离无助于
        命令匹配，故不剥离）。
    """
    if not prefixes:
        return False

    raw = raw_message_text(event)
    try:
        cur = (event.get_message_str() or "").strip()
    except Exception:  # noqa: BLE001
        cur = raw

    # 已被本群自定义前缀命中并剥离过，直接走原逻辑（不再二次处理）
    if event.get_extra("_gwake_stripped", False):
        return False

    mode = (match_mode or "prefix").lower()

    if mode == "contains":
        # 包含匹配：消息任意位置出现前缀即唤醒（不剥离）
        for p in prefixes:
            if p and p in raw:
                event.is_at_or_wake_command = True
                return False
    else:
        # 1) 开头匹配：命中自定义前缀 -> 剥离并唤醒
        matched = None
        for p in sorted(prefixes, key=len, reverse=True):
            if p and raw.startswith(p):
                matched = p
                break
        if matched:
            rest = raw[len(matched):].lstrip()
            if not rest:
                # 只有前缀、无实际内容，不唤醒
                event.is_at_or_wake_command = False
                return True
            # 只剥 message_str 上仍存在的前缀：
            # 若 WakingCheckStage 已因 wake_prefix 剥过一次，此处不应重复剥离。
            if cur.startswith(matched):
                cur = cur[len(matched):].lstrip()
            if not cur:
                # 极端情况：被双端剥空，回退用剩余原文，保证命令仍可匹配
                cur = rest
            event.message_str = cur
            event.is_at_or_wake_command = True
            event.set_extra("_gwake_stripped", True)
            return False

    # 2) 未命中自定义前缀，但可能被系统内置机制（/ 前缀、平台 wake_prefix）唤醒
    if suppress_builtin and event.is_at_or_wake_command:
        if not is_mgmt(raw):
            if is_at_or_reply_wake(event):
                # @ 提及 / @全体 / 引用回复本机器人：不屏蔽，保留对话唤醒
                # （此时不会剥离前缀，是否触发命令交由原逻辑判定）
                return False
            # 屏蔽 / 前缀与 wake_prefix 触发的命令唤醒，仅本群自定义前缀可唤醒
            event.is_at_or_wake_command = False
            return True

    return False


# ----------------------------------------------------------------- 命令过滤器 monkeypatch
#
# 重要：AstrBot 重载插件时会把插件相关模块从 ``sys.modules`` 中移除
# （见 astrbot/core/star/star_manager.py 中「从 sys.modules 中移除指定的模块」），
# 这会导致本模块的全局变量被重置。若每次重载都无脑再包一层补丁，就会形成**补丁叠加**：
# 外层用新实例（前缀已清空），内层仍持有旧实例（前缀还在），于是内层继续屏蔽，
# 表现为「/delwake 之后系统默认唤醒依旧被屏蔽、永远恢复不了」。
#
# 因此这里做两件事：
#   1) 安装前先沿闭包链向下剥离所有由本插件产生的补丁层，取回**真正的原始** filter；
#   2) 始终只保留**一层**补丁，且该层每次安装都指向**最新**的插件实例。

_PLUGIN_MODULE_PREFIX = "astrbot_plugin_group_wake_prefix"

# 识别「真正被包裹的 filter 函数」时优先按这些自由变量名取
# （本插件 v1.2.0 / v1.2.1 均将上一级 filter 命名为 ``original``；列出常见别名以便兼容）
_WRAPPED_NAMES = ("original", "_original", "orig", "wrapped", "inner", "prev", "previous")


def _is_our_func(fn) -> bool:
    """判断某个 filter 函数是否由本插件产生（用于剥离历史补丁层）。"""
    if getattr(fn, "_gwake_patch", False):
        return True
    return (getattr(fn, "__module__", "") or "").startswith(_PLUGIN_MODULE_PREFIX)


def _unwrap_original(fn):
    """沿闭包链向下找到本插件补丁函数真正包裹的 filter 函数。

    注意：本插件补丁的 ``patched_filter`` 闭包中通常含多个 callable
    （例如 ``original``/``plugin_getter``/``apply_wake_rules``），
    按字母顺序遍历会先取到本模块其它工具函数，**不是**真正被包裹的 filter。
    因此优先按自由变量名 ``original`` / ``_original`` 等取对应闭包单元，
    仅在没有约定命名时退化到「第一个非本插件定义的 callable」。
    """
    for _ in range(16):
        if not callable(fn) or not _is_our_func(fn):
            break
        freevars = getattr(fn.__code__, "co_freevars", ())
        closure = getattr(fn, "__closure__", None) or ()
        nxt = None
        # 优先按惯例命名取「真正被包裹的 filter」
        for i, name in enumerate(freevars):
            if name in _WRAPPED_NAMES and i < len(closure):
                try:
                    val = closure[i].cell_contents
                except ValueError:
                    continue
                if callable(val):
                    nxt = val
                    break
        if nxt is None:
            # 退化：取第一个 callable 且不是本插件定义的函数
            fallback = None
            for cell in closure:
                try:
                    val = cell.cell_contents
                except ValueError:
                    continue
                if not callable(val):
                    continue
                if fallback is None:
                    fallback = val
                if not _is_our_func(val):
                    nxt = val
                    break
            nxt = nxt if nxt is not None else fallback
        if nxt is None or nxt is fn:
            break
        fn = nxt
    return fn


def install_patch(command_filter_cls, plugin_getter):
    """对 AstrBot 的 ``CommandFilter`` 打补丁（幂等，可安全重复调用；兼容热重载）。

    关键改进（v1.2.5）：用类属性 ``_gwake_original_filter`` 记录「真正的原始 filter」，
    每次安装都先**强制把 CommandFilter.filter 重置回原始**，再装一层指向『当前』实例的
    补丁。这彻底避免了热重载 / 多次重载导致的「旧实例补丁层残留」——旧层可能仍持有已删除的
    前缀（如 #）并继续屏蔽 /，而新 handler 读盘显示已空，造成分裂假象。

    同时把当前生效的插件实例 id 写到 ``CommandFilter._gwake_active_plugin_id``，
    供 /wakestatus 诊断「拦截层与诊断层是否为同一实例」。
    """
    orig_key = "_gwake_original_filter"
    real_original = getattr(command_filter_cls, orig_key, None)
    if real_original is None or not callable(real_original):
        real_original = _unwrap_original(command_filter_cls.filter)
        try:
            setattr(command_filter_cls, orig_key, real_original)
        except Exception:  # noqa: BLE001
            pass

    # 强制重置：剥掉任何残留的插件补丁层，保证最终只有一层且指向当前实例
    command_filter_cls.filter = real_original

    def patched_filter(self, event, cfg):
        try:
            plugin = plugin_getter()
        except Exception:  # noqa: BLE001
            plugin = None
        try:
            setattr(
                command_filter_cls,
                "_gwake_active_plugin_id",
                id(plugin) if plugin is not None else None,
            )
        except Exception:  # noqa: BLE001
            pass
        if plugin is None:
            return real_original(self, event, cfg)

        try:
            gid = event.get_group_id()
        except Exception:  # noqa: BLE001
            gid = ""
        try:
            # 透传 event：让每条消息只落盘读一次（单条消息级缓存，见 main._get_prefixes）
            prefixes = plugin._get_prefixes(gid, event) if gid else []
        except Exception:  # noqa: BLE001
            prefixes = []

        if prefixes and apply_wake_rules(
            prefixes,
            plugin.config.get("suppress_builtin", True),
            plugin._is_mgmt_command,
            event,
            plugin.config.get("match_mode", "prefix"),
        ):
            return False
        return real_original(self, event, cfg)

    patched_filter._gwake_patch = True  # 标记，供下次安装时剥离
    command_filter_cls.filter = patched_filter
    return True


def count_patch_layers(command_filter_cls) -> int:
    """统计 CommandFilter.filter 上由本插件打补丁的层数（用于诊断）。

    通过沿闭包链走回原始 filter，每遇到一个本插件产生的函数层就 +1。
    正常应为 1；若 > 1 说明存在历史残留层，会导致 /delwake 失效。
    """
    fn = command_filter_cls.filter
    count = 0
    seen: set[int] = set()
    for _ in range(32):
        if id(fn) in seen:
            break
        seen.add(id(fn))
        if not callable(fn) or not _is_our_func(fn):
            break
        count += 1
        freevars = getattr(fn.__code__, "co_freevars", ())
        closure = getattr(fn, "__closure__", None) or ()
        nxt = None
        for i, name in enumerate(freevars):
            if name in _WRAPPED_NAMES and i < len(closure):
                try:
                    val = closure[i].cell_contents
                except ValueError:
                    continue
                if callable(val):
                    nxt = val
                    break
        if nxt is None or nxt is fn:
            break
        fn = nxt
    return count
