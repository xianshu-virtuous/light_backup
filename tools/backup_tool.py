"""light_backup 工具：给模型用的主动备份入口。

命令（``/备份``）是给人用的，工具是给模型用的。区别不只是入口——
模型会**自己判断什么时候该叫**，所以这里多了三道闸：

1. ``tools.enabled`` / ``tools.allow_export``：允不允许模型自己动手；
2. ``tools.min_interval_minutes``：太频繁就算了（模型一兴奋可能每轮都想备份，
   那会把备份目录刷满，也没意义）；
3. ``chat_type = PRIVATE``：只在私聊暴露。记忆内容属于私事，群里别人问一句
   「你备份一下」不该真的写盘。

还原**不**做成工具：那是覆盖记忆库的危险操作，只留给主人敲命令。
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import BaseTool
from src.core.components.types import ChatType

logger = get_logger("light_backup.tool")

SERVICE_SIGNATURE = "light_backup:service:core"


def _fmt_size(size: Any) -> str:
    """人类可读大小。"""
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _ago(stamp: Any) -> str:
    """多久以前（人话）。"""
    try:
        delta = max(0.0, time.time() - float(stamp or 0))
    except (TypeError, ValueError):
        return "刚刚"
    if not stamp:
        return "从没备份过"
    if delta < 60:
        return f"{int(delta)} 秒前"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


def _service(plugin: Any) -> Any:
    """取服务实例（取不到就直接实例化兜底）。"""
    try:
        instance = get_service(SERVICE_SIGNATURE)
    except Exception as exc:  # noqa: BLE001 - 取服务失败不算致命
        logger.debug(f"[light_backup] 取服务失败：{exc}")
        instance = None
    if instance is not None:
        return instance
    from ..service import LightBackupService

    return LightBackupService(plugin)


def _tools_cfg(plugin: Any) -> Any:
    """取 ``[tools]`` 配置小节。"""
    config = getattr(plugin, "config", None)
    return getattr(config, "tools", None)


class BackupNowTool(BaseTool):
    """把配置与记忆备份成一份包。"""

    tool_name = "backup_now"
    tool_description = "把配置和记忆备份成一份 .lbk 包（轻量、可校验）"

    name: str = "backup_now"
    description: str = (
        "把当前机器人的**配置 + 记忆**备份成一份 .lbk 包，存到配置好的外部目录。"
        "默认约 1~2 MB，含全部记忆条文与向量，几秒钟完成。\n"
        "什么时候用：主人说「备份一下」「存个档」「把记忆保存起来」，"
        "或者你刚做完一件重要的事、想留一个能回到此刻的记忆版本。\n"
        "不要用的情况：只是想知道备份情况 → 用 check_backup_status；"
        "刚刚备份过（10 分钟内）会被拒绝，别反复试。\n"
        "返回：包名、大小、记忆条数、耗时。"
    )
    chat_type: ChatType = ChatType.PRIVATE

    async def execute(
        self,
        reason: Annotated[
            str,
            "这次备份的原因（一句话，会记进日志便于事后对账；没有就留空）",
        ] = "",
    ) -> tuple[bool, str]:
        """执行一次备份。"""
        plugin = getattr(self, "plugin", None)
        tools_cfg = _tools_cfg(plugin)
        if tools_cfg is not None and not bool(getattr(tools_cfg, "enabled", True)):
            return False, "备份工具已被主人关闭（配置 tools.enabled = false）。"
        if tools_cfg is not None and not bool(getattr(tools_cfg, "allow_export", True)):
            return False, "主人关掉了「模型主动备份」。要备份请发 /备份 现在。"

        service = _service(plugin)

        # ── 闸门：别让模型把备份目录刷满 ─────────────────────────
        min_interval = float(getattr(tools_cfg, "min_interval_minutes", 10.0) or 0.0)
        try:
            state = await service.load_state()
            last_at = float(state.get("last_backup_at") or 0.0)
        except Exception:  # noqa: BLE001 - 读不到状态就不拦
            last_at = 0.0
        if min_interval and last_at and (time.time() - last_at) < min_interval * 60.0:
            return False, (
                f"刚备份过（{_ago(last_at)}），{min_interval:g} 分钟内不重复备份。"
                "如果确实需要，请让主人发 /备份 现在。"
            )

        note = str(reason or "").strip()[:120]
        result = await service.export(reason="tool" + (f":{note}" if note else ""))
        if not result.get("ok"):
            return False, f"备份失败：{result.get('error') or '未知错误'}"

        manifest = result.get("manifest") or {}
        stats = manifest.get("stats") or {}
        secrets_cfg = manifest.get("secrets") or {}
        time_ctx = manifest.get("time_context") or {}
        parts = [
            f"备份完成：{result.get('file')}",
            f"{_fmt_size(result.get('size'))}"
            f"｜记忆 {stats.get('records', 0)} 条"
            f"｜向量 {stats.get('vectors', 0)} 条"
            f"｜配置 {stats.get('config_files', 0)} 个"
            f"｜耗时 {result.get('elapsed', 0)}s",
        ]
        if time_ctx.get("described"):
            parts.append(f"备份时刻：{time_ctx['described']}")
        if secrets_cfg.get("redacted_keys"):
            parts.append(f"配置里 {secrets_cfg['redacted_keys']} 处密钥已脱敏")
        if result.get("extra_copy"):
            parts.append(f"另存一份：{result['extra_copy']}")
        if result.get("pruned"):
            parts.append(f"清掉旧的 {len(result['pruned'])} 份")
        logger.info(f"[light_backup] 模型主动备份完成：{result.get('file')}（{note or '无备注'}）")
        return True, "\n".join(parts)


class CheckBackupStatusTool(BaseTool):
    """查备份现状。"""

    tool_name = "check_backup_status"
    tool_description = "查备份现状：有几份、最新一份多大、上次什么时候备的"

    name: str = "check_backup_status"
    description: str = (
        "查备份现状：备份目录在哪、一共几份包、最新一份的名字和大小、上次备份时间。\n"
        "什么时候用：主人问「备份了吗」「备份到哪了」「上次什么时候备份的」。\n"
        "参数 verify_newest=true 时会顺手校验最新一份（哈希、条数、标签引用、向量维度），"
        "能回答「那份包到底能不能用来还原」——这个会多花一两秒。\n"
        "注意：这个工具**只读**，不会创建或修改任何备份。"
    )
    chat_type: ChatType = ChatType.PRIVATE

    async def execute(
        self,
        verify_newest: Annotated[
            bool,
            "是否顺手校验最新一份包（默认 false；只回答「能不能用来还原」时才需要）",
        ] = False,
    ) -> tuple[bool, str]:
        """返回备份现状。"""
        plugin = getattr(self, "plugin", None)
        tools_cfg = _tools_cfg(plugin)
        if tools_cfg is not None and not bool(getattr(tools_cfg, "enabled", True)):
            return False, "备份工具已被主人关闭（配置 tools.enabled = false）。"

        service = _service(plugin)
        try:
            status = await service.status()
        except Exception as exc:  # noqa: BLE001 - 统一翻译成给模型看的话
            return False, f"查询失败：{type(exc).__name__}: {exc}"

        lines = [
            f"备份目录：{status.get('output_dir')}"
            + ("" if status.get("output_exists") else "（还没创建）"),
            f"已有 {status.get('archive_count')} 份"
            f"，共 {_fmt_size(status.get('total_size'))}",
        ]
        if status.get("newest_name"):
            lines.append(
                f"最新：{status.get('newest_name')}"
                f"（{_fmt_size(status.get('newest_size'))}，"
                f"{status.get('last_backup_ago') or _ago(status.get('newest_mtime'))}）"
            )
            if status.get("newest_summary"):
                lines.append(f"内容：{status.get('newest_summary')}")
        else:
            lines.append("还没有备份过。")
        if status.get("now_described"):
            lines.append(f"现在：{status.get('now_described')}")
        lines.append(f"自动备份：{'每天 ' + str(status.get('daily_at')) if status.get('schedule_enabled') else '已关闭'}")
        if status.get("clock_ok") is False:
            lines.append("注意：时间源不可靠，时间戳仅供参考。")
        if status.get("last_result") == "failed":
            lines.append(f"上次失败：{status.get('last_error')}")

        if verify_newest and status.get("newest_name"):
            try:
                report = await service.verify(str(status.get("newest_name")), deep=True)
                lines.append(f"校验：{report.summary_line()}")
                for item in report.failures[:4]:
                    lines.append(f"  ✗ {item.label}：{item.detail}")
                if report.ok:
                    lines.append("这份包可以用。")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"校验失败：{type(exc).__name__}: {exc}")

        return True, "\n".join(lines)
