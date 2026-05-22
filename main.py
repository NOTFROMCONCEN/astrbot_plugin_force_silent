from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 引入全局工具（支持在开发目录和打包后两种情况）
_UTILS_PATH = Path(__file__).resolve().parent.parent / "utils"
if str(_UTILS_PATH) not in sys.path:
    sys.path.insert(0, str(_UTILS_PATH))

try:
    from command_parser import parse_command
    from config_utils import env_override, mask_key, mask_config_for_log
except Exception:
    parse_command = None  # type: ignore[misc]
    env_override = None  # type: ignore[misc]
    mask_key = None  # type: ignore[misc]
    mask_config_for_log = None  # type: ignore[misc]

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_force_silent",
    "NOTFROMCONCEN",
    "指定群号与管理员，强制 Bot 在目标群中静默（支持协同采集模式）",
    "2.2.2",
)
class ForceSilentPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.config = config or {}

        # 配置安全治理：敏感字段 ENV 覆盖
        self._load_secure_config()

        self._silent_groups_cache: set[str] = set()
        self._silent_groups_sig = ""
        self._manager_ids_cache: set[str] = set()
        self._manager_ids_sig = ""
        self._received_group_events = 0
        self._matched_silent_group_events = 0
        self._stopped_events = 0
        self._bypass_admin_events = 0
        self._bypass_coop_events = 0
        self._bypass_whitelist_events = 0
        self._max_log_line_length = self._int_conf("max_log_line_length", 512)

        self._log_verbose(
            f"startup: enabled={self._is_enabled()} cooperative_mode={self._cooperative_mode()} "
            f"silent_groups={sorted(self._silent_groups())}"
        )
        self._log_config_safe()

    def _load_secure_config(self) -> None:
        """支持 ENV 覆盖敏感字段，并确保默认值安全。"""
        if env_override is not None:
            # 目前 force_silent 暂无高敏感字段需要 ENV 覆盖，保留接口一致性
            pass

    def _log_config_safe(self) -> None:
        safe = (
            mask_config_for_log(self.config)
            if mask_config_for_log is not None
            else self.config
        )
        self._log_verbose(f"config_safe={safe}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def enforce_silent(self, event: AstrMessageEvent):
        """在配置的群中执行静默策略。"""
        self._received_group_events += 1

        if not self._is_enabled():
            return

        try:
            group_id = self._normalize(event.get_group_id())
        except Exception:
            return
        if not group_id:
            return

        if group_id in self._silent_groups():
            self._matched_silent_group_events += 1

            # 允许管理员在静默群里发送管理指令，避免“锁死”无法改配置。
            if self._is_admin_command(event):
                self._bypass_admin_events += 1
                self._log_verbose("admin command bypassed in silent group")
                return

            # 白名单命令前缀放行
            if self._is_whitelisted_command(event):
                self._bypass_whitelist_events += 1
                self._log_verbose("whitelist command bypassed in silent group")
                return

            # 协同模式：不 stop_event，让采集插件仍可落库。
            if self._cooperative_mode():
                self._bypass_coop_events += 1
                self._log_verbose(f"cooperative mode pass-through in group: {group_id}")
                return

            event.stop_event()
            self._stopped_events += 1
            if self._verbose_log_enabled():
                logger.info(f"[force_silent] blocked outgoing flow in group: {group_id}")

    @filter.command("force_silent")
    async def force_silent(self, event: AstrMessageEvent):
        """管理强制静默: /强制静默 [状态|统计|开启|关闭|添加群|删除群|协同开启|协同关闭] [群号]"""

        if not self._is_manager(event):
            yield event.plain_result("无权限：你不是本插件管理员")
            return

        raw = event.message_str or ""
        if parse_command is not None:
            parsed = parse_command(raw)
            action = parsed.arg(0, "status").lower()
            arg = self._normalize(parsed.arg(1))
        else:
            tokens = [t for t in raw.strip().split() if t]
            action = "status"
            arg = ""
            if len(tokens) >= 2:
                action = tokens[1].lower()
            if len(tokens) >= 3:
                arg = self._normalize(tokens[2])

        if action in {"status", "状态", "狀態"}:
            yield event.plain_result(self._status_text())
            return

        if action in {"统计", "統計", "stats"}:
            yield event.plain_result(self._stats_text())
            return

        if action in {"on", "开启", "開啟"}:
            self.config["enabled"] = True
            self._save_config()
            yield event.plain_result("强制静默已开启")
            return

        if action in {"off", "关闭", "關閉"}:
            self.config["enabled"] = False
            self._save_config()
            yield event.plain_result("强制静默已关闭")
            return

        if action in {"add_group", "添加群"}:
            if not arg:
                yield event.plain_result("用法: /强制静默 添加群 <群号>")
                return
            groups = set(self._silent_groups())
            groups.add(arg)
            self.config["silent_group_ids"] = sorted(groups)
            self._silent_groups_sig = ""
            self._save_config()
            yield event.plain_result(f"已添加静默群: {arg}")
            return

        if action in {"del_group", "remove_group", "删除群", "刪除群"}:
            if not arg:
                yield event.plain_result("用法: /强制静默 删除群 <群号>")
                return
            groups = set(self._silent_groups())
            groups.discard(arg)
            self.config["silent_group_ids"] = sorted(groups)
            self._silent_groups_sig = ""
            self._save_config()
            yield event.plain_result(f"已移除静默群: {arg}")
            return

        if action in {"协同开启", "協同開啟", "co_on", "coop_on"}:
            self.config["cooperative_mode"] = True
            self._save_config()
            yield event.plain_result(
                "协同模式已开启（不 stop_event，允许采集插件接收消息）"
            )
            return

        if action in {"协同关闭", "協同關閉", "co_off", "coop_off"}:
            self.config["cooperative_mode"] = False
            self._save_config()
            yield event.plain_result("协同模式已关闭（恢复 stop_event 硬静默）")
            return

        yield event.plain_result(
            "未知操作。用法: /强制静默 [状态|统计|开启|关闭|添加群 <群号>|删除群 <群号>|协同开启|协同关闭]"
        )

    @filter.command("强制静默")
    async def force_silent_cn(self, event: AstrMessageEvent):
        """中文管理指令入口。"""
        async for result in self.force_silent(event):
            yield result

    @filter.command("強制靜默")
    async def force_silent_tw(self, event: AstrMessageEvent):
        """繁體中文管理指令入口。"""
        async for result in self.force_silent(event):
            yield result

    def _is_enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def _cooperative_mode(self) -> bool:
        return bool(self.config.get("cooperative_mode", True))

    def _verbose_log_enabled(self) -> bool:
        return bool(self.config.get("verbose_log", True))

    def _silent_groups(self) -> set[str]:
        data = self.config.get("silent_group_ids", []) or []
        normalized: list[str] = []
        for item in data:
            v = self._normalize(item)
            if v:
                normalized.append(v)
        sig = "|".join(sorted(normalized))
        if sig != self._silent_groups_sig:
            self._silent_groups_sig = sig
            self._silent_groups_cache = set(normalized)
        return self._silent_groups_cache

    def _manager_ids(self) -> set[str]:
        data = self.config.get("admin_user_ids", []) or []
        normalized: list[str] = []
        for item in data:
            v = self._normalize(item)
            if v:
                normalized.append(v)
        sig = "|".join(sorted(normalized))
        if sig != self._manager_ids_sig:
            self._manager_ids_sig = sig
            self._manager_ids_cache = set(normalized)
        return self._manager_ids_cache

    def _is_manager(self, event: AstrMessageEvent) -> bool:
        sender_id = self._normalize(event.get_sender_id())
        if sender_id in self._manager_ids():
            return True

        if bool(self.config.get("allow_astrbot_admin", True)):
            try:
                return bool(event.is_admin())
            except Exception:
                return False

        return False

    def _status_text(self) -> str:
        enabled = "开启" if self._is_enabled() else "关闭"
        mode = (
            "协同（不截断事件）" if self._cooperative_mode() else "硬静默（stop_event）"
        )
        groups = ", ".join(sorted(self._silent_groups())) or "无"
        admins = ", ".join(sorted(self._manager_ids())) or "无"
        wl = self._whitelist_prefixes()
        wl_text = ", ".join(wl) or "无"

        # 策略优先级显式化
        will_stop = self._will_stop_event_now()
        stop_reason = self._stop_event_reason()

        return (
            "[强制静默]\n"
            f"- 开关: {enabled}\n"
            f"- 模式: {mode}\n"
            f"- 静默群: {groups}\n"
            f"- 插件管理员: {admins}\n"
            f"- 白名单前缀: {wl_text}\n"
            f"- 当前会 stop_event: {'是' if will_stop else '否'}\n"
            f"- 原因: {stop_reason}\n"
            "- 指令: /强制静默 [状态|统计|开启|关闭|添加群 <群号>|删除群 <群号>|协同开启|协同关闭]"
        )

    def _stats_text(self) -> str:
        return (
            "[强制静默-统计]\n"
            f"- runtime_received_group_events: {self._received_group_events}\n"
            f"- runtime_matched_silent_group_events: {self._matched_silent_group_events}\n"
            f"- runtime_stopped_events: {self._stopped_events}\n"
            f"- runtime_bypass_admin: {self._bypass_admin_events}\n"
            f"- runtime_bypass_coop: {self._bypass_coop_events}\n"
            f"- runtime_bypass_whitelist: {self._bypass_whitelist_events}\n"
            f"- cooperative_mode: {self._cooperative_mode()}"
        )

    def _is_admin_command(self, event: AstrMessageEvent) -> bool:
        if not self._is_manager(event):
            return False
        text = self._normalize(event.message_str)
        return (
            text.startswith("/强制静默")
            or text.startswith("/強制靜默")
            or text.startswith("/force_silent")
        )

    def _whitelist_prefixes(self) -> list[str]:
        raw = self.config.get("whitelist_command_prefixes", [])
        if isinstance(raw, list):
            return [self._normalize(p) for p in raw if self._normalize(p)]
        if isinstance(raw, str):
            return [p.strip() for p in raw.split(",") if p.strip()]
        return []

    def _is_whitelisted_command(self, event: AstrMessageEvent) -> bool:
        text = self._normalize(event.message_str)
        if not text:
            return False
        for prefix in self._whitelist_prefixes():
            if text.startswith(prefix):
                return True
        return False

    def _will_stop_event_now(self) -> bool:
        """基于当前配置，判断若命中静默群是否会 stop_event。"""
        if not self._is_enabled():
            return False
        if self._cooperative_mode():
            return False
        return bool(self._silent_groups())

    def _stop_event_reason(self) -> str:
        if not self._is_enabled():
            return "插件已关闭"
        if not self._silent_groups():
            return "无静默群"
        if self._cooperative_mode():
            return "协同模式开启，放行事件"
        return "硬静默模式，命中静默群时 stop_event"

    def _save_config(self):
        save_fn = getattr(self.config, "save_config", None)
        if callable(save_fn):
            save_fn()

    def _int_conf(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except Exception:
            return default

    def _log_verbose(self, text: str):
        if not self._verbose_log_enabled():
            return
        msg = f"[force_silent] {text}"
        if len(msg) > self._max_log_line_length:
            msg = msg[: self._max_log_line_length] + "...[truncated]"
        logger.info(msg)

    @staticmethod
    def _normalize(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    async def terminate(self):
        logger.info("[force_silent] terminated")


