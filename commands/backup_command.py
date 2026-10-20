"""``/备份`` 命令：现在备份一份、看看有哪些、验一下能不能用。

仅主人可用（``PermissionLevel.OWNER``）——它碰的是实例的记忆库与配置。

```
/备份                 状态总览
/备份 现在            立刻备份一份
/备份 列表            最近几份包
/备份 校验 [名字]     校验（默认最新一份）
/备份 帮助
```
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.api.service_api import get_service
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel

logger = get_logger("light_backup.command")

SERVICE_SIGNATURE = "light_backup:service:core"

_HELP = """【轻量备份】
/备份              状态总览
/备份 现在         立刻备份一份
/备份 列表         最近几份包
/备份 校验 [名字]  校验包（默认最新）
/备份 帮助         本帮助

备份内容：记忆（含向量）+ 配置（密钥脱敏）+ 知识库
还原开关默认关闭，v1.0 只做导出与校验。"""


def _fmt_size(size: int) -> str:
    """人类可读大小。"""
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _fmt_time(stamp: float) -> str:
    """时间戳转可读文本。"""
    if not stamp:
        return "从未"
    try:
        return datetime.fromtimestamp(float(stamp)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def _ago(stamp: float) -> str:
    """多久以前。"""
    if not stamp:
        return ""
    delta = max(0.0, time.time() - float(stamp))
    if delta < 60:
        return f"{int(delta)} 秒前"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


class BackupCommand(BaseCommand):
    """轻量备份命令。"""

    name: str = "备份"
    description: str = "轻量备份：配置 + 记忆（含向量）导出与校验"
    permission_level: PermissionLevel = PermissionLevel.OWNER

    @classmethod
    def match(cls, parts: list[str]) -> int:
        """匹配命令名（``备份`` / ``backup``）。"""
        if not parts:
            return 0
        return 1 if parts[0] in ("备份", "backup") else 0

    # ── 内部 ─────────────────────────────────────────────────────
    async def _reply(self, text: str) -> None:
        """回一条消息到当前流。"""
        await send_text(text, stream_id=self.stream_id)

    def _service(self) -> Any:
        """取本插件的服务实例（取不到就直接实例化兜底）。"""
        try:
            service = get_service(SERVICE_SIGNATURE)
        except Exception as exc:  # noqa: BLE001 - 取服务失败不算致命
            logger.debug(f"[light_backup] 取服务失败：{exc}")
            service = None
        if service is not None:
            return service
        from ..service import LightBackupService

        return LightBackupService(self.plugin)

    # ── 路由 ─────────────────────────────────────────────────────
    @cmd_route()
    async def handle_root(self) -> tuple[bool, str]:
        """无参数 = 状态总览。"""
        return await self.handle_status()

    @cmd_route("状态")
    async def handle_status(self) -> tuple[bool, str]:
        """状态总览。"""
        service = self._service()
        status = await service.status()

        last = status.get("last_backup_at") or 0.0
        result = status.get("last_result") or ""
        lines = [
            "【轻量备份】",
            f"实例：{status.get('instance_root')}",
            f"目录：{status.get('output_dir')}"
            + ("" if status.get("output_exists") else "（还没创建）"),
            f"档位：{status.get('tier')}"
            + (f"/{status.get('vector_precision')}" if status.get("tier") == "full" else ""),
            f"自动：{'每' + str(status.get('daily_at')) if status.get('schedule_enabled') else '已关闭'}",
            f"已有：{status.get('archive_count')} 份，共 {_fmt_size(status.get('total_size') or 0)}",
        ]
        if status.get("newest_name"):
            lines.append(
                f"最新：{status.get('newest_name')}"
                f"（{_fmt_size(status.get('newest_size') or 0)}，{_ago(status.get('newest_mtime') or 0)}）"
            )
        if last:
            lines.append(f"上次：{_fmt_time(last)} {_ago(last)} {result}")
        if result == "failed" and status.get("last_error"):
            lines.append(f"上次错误：{status.get('last_error')}")
        lines.append("命令：/备份 现在｜列表｜校验")
        text = "\n".join(lines)
        await self._reply(text)
        return True, text

    @cmd_route("现在")
    async def handle_now(self) -> tuple[bool, str]:
        """立刻备份一份。"""
        service = self._service()
        await self._reply("开始备份，稍等…")
        result = await service.export(reason="manual")

        if not result.get("ok"):
            text = f"备份失败：{result.get('error') or '未知错误'}"
            await self._reply(text)
            return False, text

        manifest = result.get("manifest") or {}
        stats = manifest.get("stats") or {}
        parts = [
            f"✅ 备份完成：{result.get('file')}",
            f"{_fmt_size(result.get('size') or 0)}｜{stats.get('records', 0)} 条记忆"
            f"｜向量 {stats.get('vectors', 0)} 条"
            f"｜配置 {stats.get('config_files', 0)} 个"
            f"｜耗时 {result.get('elapsed', 0)}s",
        ]
        if (manifest.get("secrets") or {}).get("redacted_keys"):
            parts.append(f"配置里 {manifest['secrets']['redacted_keys']} 处密钥已脱敏")
        if result.get("extra_copy"):
            parts.append(f"额外副本：{result['extra_copy']}")
        if result.get("pruned"):
            parts.append(f"按保留份数清掉旧包 {len(result['pruned'])} 份")
        text = "\n".join(parts)
        await self._reply(text)
        return True, text

    @cmd_route("列表")
    async def handle_list(self, limit: str = "") -> tuple[bool, str]:
        """列出备份包。"""
        service = self._service()
        listing = await service.listing()
        items = listing.get("items") or []
        if not items:
            text = f"还没有备份包。\n目录：{listing.get('output_dir')}"
            await self._reply(text)
            return True, text

        try:
            show = max(1, min(int(str(limit or "5") or 5), 20))
        except ValueError:
            show = 5

        lines = [f"【备份包】{len(items)} 份｜{listing.get('output_dir')}"]
        for item in items[:show]:
            manifest = item.get("manifest") or {}
            stats = manifest.get("stats") or {}
            lines.append(
                f"- {item.get('name')}\n"
                f"  {_fmt_size(item.get('size') or 0)}｜{_fmt_time(item.get('mtime') or 0)}"
                f"｜{stats.get('records', 0)} 条记忆"
            )
        if len(items) > show:
            lines.append(f"…还有 {len(items) - show} 份")
        text = "\n".join(lines)
        await self._reply(text)
        return True, text

    @cmd_route("校验")
    async def handle_verify(self, name: str = "") -> tuple[bool, str]:
        """校验一份包（默认最新）。"""
        service = self._service()
        target = str(name or "").strip()
        if not target:
            listing = await service.listing()
            items = listing.get("items") or []
            if not items:
                text = "还没有备份包可以校验。"
                await self._reply(text)
                return False, text
            target = str(items[0].get("name") or "")

        await self._reply(f"校验中：{target}")
        report = await service.verify(target, deep=True)

        lines = [f"【校验】{target}", report.summary_line()]
        for item in report.checks:
            mark = "✓" if item.ok else "✗"
            lines.append(f"{mark} {item.label}" + (f"：{item.detail}" if item.detail else ""))
        if report.manifest:
            from ..verify import describe_manifest

            lines.append(describe_manifest(report.manifest))
        if not report.ok:
            lines.append("这份包不能用来还原，先看看上面 ✗ 的项。")
        text = "\n".join(lines)
        await self._reply(text)
        return report.ok, text

    @cmd_route("帮助")
    async def handle_help(self) -> tuple[bool, str]:
        """帮助。"""
        await self._reply(_HELP)
        return True, _HELP

    @cmd_route("now")
    async def handle_now_en(self) -> tuple[bool, str]:
        """英文别名：now。"""
        return await self.handle_now()

    @cmd_route("list")
    async def handle_list_en(self, limit: str = "") -> tuple[bool, str]:
        """英文别名：list。"""
        return await self.handle_list(limit)

    @cmd_route("verify")
    async def handle_verify_en(self, name: str = "") -> tuple[bool, str]:
        """英文别名：verify。"""
        return await self.handle_verify(name)

    @cmd_route("help")
    async def handle_help_en(self) -> tuple[bool, str]:
        """英文别名：help。"""
        return await self.handle_help()
