"""群唤醒前缀自定义插件（v1.4.2）。

在不同群里通过指令自定义专属唤醒符号 / 词语，实现按群区分的唤醒方式；
并支持在本群设置自定义前缀后屏蔽系统内置的「命令唤醒」（/ 前缀、平台 wake_prefix），
同时保留 @ 提及与引用回复等社交性唤醒；清除自定义前缀后自动恢复内置唤醒。

实现要点（详见 group_wake_rules.py 注释）：
AstrBot 命令激活发生在 WakingCheckStage，插件 handler 无法在此之前介入，
因此本插件通过 monkeypatch ``CommandFilter.filter``（命令激活的统一入口）来改写
event 的唤醒状态，从而同时实现「自定义前缀触发命令」与「屏蔽内置命令唤醒」。
"""

import re
import json
import asyncio
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools
from astrbot import logger

from .group_wake_rules import (
    install_patch,
    apply_wake_rules,
    is_mgmt_command,
    merge_prefixes,
    remove_prefixes,
    count_patch_layers,
    load_prefixes_from_file,
)

PLUGIN_NAME = "astrbot_plugin_group_wake_prefix"
DEFAULT_REPO = "https://github.com/yabaiqaq/astrbot_plugin_group_wake_prefix"


@register(
    PLUGIN_NAME,
    "yabaiqaq",
    "在不同群内通过指令自定义专属唤醒前缀（符号或词），支持屏蔽/恢复系统内置唤醒方式",
    "1.4.2",
    DEFAULT_REPO,
)
class GroupWakePrefixPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self._lock = asyncio.Lock()
        self._data_file = StarTools.get_data_dir(PLUGIN_NAME) / "group_prefixes.json"

        # 安装命令过滤器补丁：让「自定义前缀触发命令」与「屏蔽内置命令唤醒」真正生效。
        # 必须在运行时（已具备 astrbot 环境）才 import，避免无 astrbot 环境下加载失败。
        try:
            from astrbot.core.star.filter.command import CommandFilter

            install_patch(CommandFilter, lambda: self)
            logger.info(
                f"[{PLUGIN_NAME}] 已安装命令过滤器补丁 "
                f"(plugin id={id(self)}, layers={count_patch_layers(CommandFilter)})"
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"[{PLUGIN_NAME}] 安装命令过滤器补丁失败，插件将无法按预期工作: {e}"
            )

    # ------------------------------------------------------------------ 持久化
    # 设计要点：磁盘 JSON 是唯一真相来源，绝不维护任何内存缓存。
    # 这样无论插件被重载多少次、补丁闭包捕获的是哪个实例，
    # patched_filter（命令激活前的拦截）与 wakestatus（诊断）读到的永远一致 ——
    # 不会出现「wakestatus 显示已清空、默认唤醒却仍被屏蔽」的分裂。
    # 性能：用「单条消息级」缓存（存到 event extra），一条消息对多个命令的
    # 多次 filter 调用只落盘读一次。

    async def _save_data(self, data: dict) -> None:
        """原子写入整份配置（文件很小，整份覆盖即可）。"""
        self._data_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._data_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._data_file)

    def _read_all(self) -> dict:
        """读取磁盘上的整份配置（返回 dict）。"""
        try:
            if self._data_file.exists():
                d = json.loads(self._data_file.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _get_prefixes(self, gid: str, event=None) -> list[str]:
        """读取本群前缀：磁盘为唯一真相，并借用 event 做单条消息级缓存。"""
        if event is not None:
            cached = event.get_extra("_gwake_prefixes")
            if isinstance(cached, list):
                return cached
        prefixes = load_prefixes_from_file(str(self._data_file), gid)
        if event is not None:
            event.set_extra("_gwake_prefixes", prefixes)
        return prefixes

    async def _set_prefixes(self, gid: str, prefixes: list[str]) -> None:
        async with self._lock:
            data = self._read_all()
            if prefixes:
                data[gid] = list(prefixes)
            else:
                data.pop(gid, None)
            await self._save_data(data)

    # --------------------------------------------------------- AstrBot 环境读取
    def _astrbot_config(self) -> dict | None:
        """读取 AstrBot 主配置（拿不到时返回 None，诊断命令会标注「无法读取」）。"""
        for getter in ("get_config", "get_astrbot_config"):
            fn = getattr(self.context, getter, None)
            if callable(fn):
                try:
                    cfg = fn()
                    if isinstance(cfg, dict):
                        return cfg
                except Exception:  # noqa: BLE001
                    continue
        for attr in ("_config", "astrbot_config"):
            cfg = getattr(self.context, attr, None)
            if isinstance(cfg, dict):
                return cfg
        return None

    def _wake_prefixes(self) -> list[str]:
        cfg = self._astrbot_config()
        if not isinstance(cfg, dict):
            return []
        raw = cfg.get("wake_prefix", [])
        if isinstance(raw, (list, tuple)):
            return [str(p) for p in raw if str(p).strip()]
        return [str(raw)] if str(raw).strip() else []

    def _hint_prefix(self) -> str:
        """取 AstrBot 实际生效的首选唤醒前缀，用于提示文案。

        避免一律写「/setwake」却在用户环境里 / 并非唤醒前缀，导致照抄也发不出去。
        """
        wps = self._wake_prefixes()
        return wps[0] if wps else "/"

    def _registered_commands(self) -> list[str] | None:
        """列出当前已注册的命令名（含别名）；读取失败返回 None。"""
        try:
            from astrbot.core.star.filter.command import CommandFilter
            from astrbot.core.star.star_handler import (
                EventType,
                star_handlers_registry,
            )

            handlers = star_handlers_registry.get_handlers_by_event_type(
                EventType.AdapterMessageEvent,
                plugins_name=None,
            )
            names: set[str] = set()
            for h in handlers:
                for f in getattr(h, "event_filters", None) or []:
                    if isinstance(f, CommandFilter):
                        n = getattr(f, "command_name", None)
                        if n:
                            names.add(str(n))
                        for a in getattr(f, "alias", None) or ():
                            names.add(str(a))
            return sorted(names)
        except Exception:  # noqa: BLE001
            return None

    # -------------------------------------------------------------------- 权限
    def _can_set(self, event: AstrMessageEvent) -> bool:
        if self.config.get("allow_member_set", False):
            return True
        return event.is_admin()

    def _is_mgmt_command(self, text: str) -> bool:
        return is_mgmt_command(text)

    # ----------------------------------------------------------------- 指令设置
    @filter.command("setwake")
    async def set_wake(self, event: AstrMessageEvent):
        if event.is_private_chat():
            yield event.plain_result("请在群聊中使用 /setwake 设置本群的唤醒前缀。")
            return
        if not self._can_set(event):
            yield event.plain_result(
                "只有管理员可以设置本群唤醒前缀（或在插件配置中开启 allow_member_set）。"
            )
            return
        gid = event.get_group_id()
        raw = (event.message_str or "").strip()
        content = re.sub(r"^(/)?setwake[\s:：]*", "", raw, flags=re.IGNORECASE).strip()
        parts = content.split()
        if not parts:
            wp = self._hint_prefix()
            yield event.plain_result(
                f"用法：{wp}setwake <前缀1> [前缀2 ...]（多次设置累积，不覆盖）\n"
                f"移除：{wp}delwake <前缀> 删指定，不带参数清除全部"
            )
            return
        # 新增模式：在现有前缀基础上追加，不清空之前设置的前缀
        current = self._get_prefixes(gid)
        was_empty = not current
        merged = merge_prefixes(current, parts)
        await self._set_prefixes(gid, merged)
        names = "、".join(f"「{p}」" for p in parts)
        all_names = "、".join(f"「{p}」" for p in merged)
        lines = [f"已新增 {names}；当前本群唤醒词：{all_names}"]
        # 仅首次设置时提示屏蔽行为，避免每次操作刷屏
        if was_empty and self.config.get("suppress_builtin", True):
            lines.append("已屏蔽内置命令唤醒（/ 与 wake_prefix）；@ 与引用回复不受影响")
        # 仅当新增前缀与 wake_prefix 重叠时提示，避免误以为被屏蔽
        overlap = [p for p in parts if p in self._wake_prefixes()]
        if overlap:
            lines.append("「" + "」「".join(overlap) + "」与 wake_prefix 相同，不会误屏蔽")
        yield event.plain_result("\n".join(lines))

    @filter.command("delwake")
    async def del_wake(self, event: AstrMessageEvent):
        """清除本群唤醒前缀。

        无参数 = 清除全部（恢复系统内置唤醒）；
        带参数 = 只移除指定的前缀，支持批量（/delwake a b c），其余保留。
        """
        wp = self._hint_prefix()
        if event.is_private_chat():
            yield event.plain_result(f"请在群聊中使用 {wp}delwake 清除本群的唤醒前缀。")
            return
        if not self._can_set(event):
            yield event.plain_result("只有管理员可以清除本群唤醒前缀。")
            return
        gid = event.get_group_id()
        current = self._get_prefixes(gid)
        if not current:
            yield event.plain_result("本群原本就没有自定义唤醒前缀。")
            return
        raw = (event.message_str or "").strip()
        content = re.sub(r"^(/)?delwake[\s:：]*", "", raw, flags=re.IGNORECASE).strip()
        removals = content.split()
        if not removals:
            # 无参数：清除全部（原行为）
            await self._set_prefixes(gid, [])
            yield event.plain_result("已清除本群唤醒前缀，系统内置唤醒方式已恢复。")
            return
        # 带参数：批量移除指定前缀，其余保留
        remaining = remove_prefixes(current, removals)
        not_found = [r for r in removals if r not in current]
        await self._set_prefixes(gid, remaining)
        removed_names = "、".join(f"「{p}」" for p in removals)
        if remaining:
            lines = [f"已移除 {removed_names}；剩余：{'、'.join(f'「{p}」' for p in remaining)}"]
        else:
            lines = [f"已移除 {removed_names}；本群已无自定义唤醒前缀，已恢复内置唤醒"]
        if not_found:
            lines.append("（不存在，已忽略：" + "、".join(f"「{p}」" for p in not_found) + "）")
        yield event.plain_result("\n".join(lines))

    @filter.command("wakereset")
    async def wake_reset(self, event: AstrMessageEvent):
        """强制重置：清空所有自定义前缀 + 重装命令拦截补丁（处理热重载残留）。

        当插件被「热重载」而非「重启进程」更新时，AstrBot 只会替换 handler 方法，
        不会重新执行 install_patch——于是命令拦截层仍是旧实例（可能还攥着已删除的
        前缀如 #）并继续屏蔽 /，而 /wakestatus 读盘显示已空，造成「清了没恢复」的假象。
        本指令手动删除磁盘文件并重装拦截补丁，使其重新指向当前实例，无需重启进程。
        """
        wp = self._hint_prefix()
        if event.is_private_chat():
            yield event.plain_result(f"请在群聊中使用 {wp}wakereset。")
            return
        if not self._can_set(event):
            yield event.plain_result("只有管理员可以执行唤醒重置。")
            return
        # 1) 清空所有群自定义前缀（删除磁盘文件）
        try:
            if self._data_file.exists():
                self._data_file.unlink()
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"清空数据文件失败：{e}\n请直接重启 AstrBot 进程。")
            return
        # 2) 强制重装补丁，使其指向当前实例（剥掉任何残留的旧层）
        try:
            from astrbot.core.star.filter.command import CommandFilter

            install_patch(CommandFilter, lambda: self)
            active = getattr(CommandFilter, "_gwake_active_plugin_id", None)
            layers = count_patch_layers(CommandFilter)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(
                f"重装命令拦截补丁失败：{e}\n请直接重启 AstrBot 进程后再试。"
            )
            return
        yield event.plain_result(
            "已重置：清空全部自定义前缀并刷新命令拦截层"
            f"（拦截层 id={active}，补丁层数={layers}）。\n"
            "内置唤醒（wake_prefix / @ 提及 / 私聊）已恢复；若仍异常请重启 AstrBot。"
        )

    @filter.command("wakeprefix")
    async def show_wake(self, event: AstrMessageEvent):
        wp = self._hint_prefix()
        if event.is_private_chat():
            yield event.plain_result(f"请在群聊中使用 {wp}wakeprefix 查看本群的唤醒前缀。")
            return
        gid = event.get_group_id()
        prefixes = list(dict.fromkeys(self._get_prefixes(gid)))  # 显示层去重（兼容历史脏数据）
        if not prefixes:
            yield event.plain_result("本群未设置自定义唤醒前缀，使用系统默认唤醒。")
        else:
            names = "、".join(f"「{p}」" for p in prefixes)
            mode = (self.config.get("match_mode") or "prefix").lower()
            mode_desc = "开头匹配" if mode == "prefix" else "包含匹配"
            suppress = self.config.get("suppress_builtin", True)
            suppress_desc = (
                "已屏蔽内置命令唤醒（@ 与引用回复不受影响）"
                if suppress
                else "内置唤醒未屏蔽（@ 等仍可用）"
            )
            yield event.plain_result(f"本群唤醒前缀：{names}（{mode_desc}）；{suppress_desc}")

    @filter.command("wakestatus")
    async def wake_status(self, event: AstrMessageEvent):
        """诊断命令（仅管理员）：打印补丁状态 + AstrBot 唤醒配置 + 命令注册情况。

        支持带一个参数查具体命令，例如 ``/wakestatus stats``：
        会直接回答「该命令是否注册」「'/xxx' 能否被唤醒」，用于定位
        「清除了自定义前缀后默认唤醒没恢复」到底是插件问题还是环境配置问题。
        """
        if event.is_private_chat():
            yield event.plain_result("请在群聊中使用 /wakestatus。")
            return
        if not self._can_set(event):
            yield event.plain_result("只有管理员可以查看本群唤醒诊断信息。")
            return

        gid = event.get_group_id()
        this_group = self._get_prefixes(gid)  # 经磁盘同步后的实时值
        query = self._extract_query(event).lower()

        # ---- 本插件状态 ----
        try:
            from astrbot.core.star.filter.command import CommandFilter

            layers = count_patch_layers(CommandFilter)
            active_id = getattr(CommandFilter, "_gwake_active_plugin_id", None)
        except Exception as e:  # noqa: BLE001
            layers = f"无法读取 ({e})"
            active_id = None

        suppress = self.config.get("suppress_builtin", True)
        mode = (self.config.get("match_mode") or "prefix").lower()
        gid_label = f"{gid[:6]}…({len(gid)}字符)" if len(gid) > 8 else gid

        # 磁盘 JSON 真相（这是 /delwake 后默认唤醒能否恢复的唯一判据）
        try:
            disk_exists = self._data_file.exists()
            disk_mtime = self._data_file.stat().st_mtime if disk_exists else None
            disk_raw = (
                self._data_file.read_text(encoding="utf-8") if disk_exists else ""
            )
            disk_content = disk_raw if disk_raw.strip() else "（空文件）"
        except Exception as e:  # noqa: BLE001
            disk_exists = None
            disk_mtime = None
            disk_content = f"无法读取 ({e})"

        same_instance = (active_id is not None and active_id == id(self))
        lines = [
            "【本插件】",
            f"插件实例 id：{id(self)}",
            f"CommandFilter 补丁层数：{layers}（>1 为异常残留）",
            f"拦截层生效实例 id：{active_id}"
            + ("（与诊断实例一致 ✓）" if same_instance else "（⚠️ 与诊断实例不一致=历史残留）"),
            f"本群（{gid_label}）实时前缀(经磁盘)：{this_group or '[]'}",
            f"suppress_builtin：{suppress}（仅屏蔽 / 与 wake_prefix 命令唤醒；@ 提及、引用回复不受影响） / match_mode：{mode}",
            f"数据文件：{self._data_file}",
            f"  存在={disk_exists}  mtime={disk_mtime}",
            f"  文件内容：{disk_content}",
        ]

        # ---- AstrBot 唤醒配置（这一节是「默认唤醒」的真相）----
        wps = self._wake_prefixes()
        cfg = self._astrbot_config()
        if cfg is None:
            lines.append("【AstrBot】wake_prefix：无法读取（请查控制台日志）")
        else:
            disable_builtin = cfg.get("disable_builtin_commands", False)
            lines.append("【AstrBot】")
            lines.append(f"wake_prefix：{wps or '[]（空）'}")
            lines.append(f"'/' 是唤醒前缀：{'是' if '/' in wps else '否 ← /xxx 不会唤醒'}")
            lines.append(f"disable_builtin_commands：{disable_builtin}")

        # ---- 本次消息实际状态 ----
        try:
            has_at = any(type(m).__name__ == "At" for m in event.get_messages())
        except Exception:  # noqa: BLE001
            has_at = None
        lines.append("【本次消息】")
        lines.append(f"is_at_or_wake_command：{event.is_at_or_wake_command}")
        lines.append(f"含 @ 消息段：{has_at}")
        lines.append(f"message_str：{(event.message_str or '')[:40]!r}")

        # ---- 命令注册情况 ----
        cmds = self._registered_commands()
        if cmds is None:
            lines.append("【命令表】无法读取已注册命令")
        else:
            lines.append(f"【命令表】已注册命令数：{len(cmds)}")
            if query:
                hit = [c for c in cmds if query in c.lower()]
                if hit:
                    lines.append(f"含「{query}」的命令：{'、'.join(hit[:12])}")
                    would_wake = any(f"/{query}".startswith(p) for p in wps)
                    lines.append(
                        f"'/{query}' 能否唤醒：{'能' if would_wake else '不能（/ 不在 wake_prefix 中）'}"
                    )
                else:
                    lines.append(
                        f"未找到含「{query}」的命令 → 该指令本身不存在，"
                        f"与唤醒配置无关"
                    )
            else:
                lines.append("样例：" + "、".join(cmds[:15]))

        lines.append("提示：/wakestatus <命令名> 可查具体指令是否存在。")
        yield event.plain_result("\n".join(lines))

    def _extract_query(self, event: AstrMessageEvent) -> str:
        """从 /wakestatus 的消息里取出可选的命令名参数。"""
        raw = (event.get_message_str() or "").strip()
        m = re.match(r"^/?wakestatus[\s:：]*(\S*)", raw, flags=re.IGNORECASE)
        return (m.group(1) if m else "").strip().lstrip("/")
