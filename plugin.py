"""light_backup 插件入口。

轻量备份：**配置 + 记忆**，默认存实例外面，按天自动跑。

三个设计上的硬口径：

1. **只读实例数据**：记忆库用 ``mode=ro`` 打开，配置只读文本，向量只解当前有效项。
   备份过程不会写、锁、改任何实例文件 —— bot 照常在跑。
2. **密钥默认脱敏**：配置里命中的键值写成 ``<redacted>`` 再进包。
3. **还原默认关闭**：v1.0 只做「导出 + 校验」，``restore.allow`` 打开才写库。

定时不用 cron 库：注册一个 300 秒的巡检任务，每次看看「今天这个点过了没」。
只要进程在跑，那一天一定会被检查到一次；错过点（当时没开机）也会在启动后补上。
"""

from __future__ import annotations

import asyncio
import time
from datetime import date
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin
from src.kernel.scheduler import TriggerType, get_unified_scheduler

from .commands import BackupCommand
from .config import LightBackupConfig
from .service import LightBackupService

logger = get_logger("light_backup")

#: 巡检任务名（固定，重载时覆盖旧任务）。
_TICK_TASK_NAME = "light_backup_tick"

#: 巡检间隔（秒）。备份是每天一次的事，没必要更密。
_TICK_SECONDS = 300

#: 等调度器就绪的最大轮数（每轮 0.5s）。
_SCHEDULER_WAIT_ROUNDS = 600


def _parse_daily_at(raw: str) -> tuple[int, int] | None:
    """把 ``HH:MM`` 解析成 ``(时, 分)``；非法返回 ``None``。"""
    text = str(raw or "").strip()
    if not text or ":" not in text:
        return None
    hour_text, minute_text = text.split(":", 1)
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


@register_plugin
class LightBackupPlugin(BasePlugin):
    """轻量备份插件。"""

    plugin_name: str = "light_backup"
    plugin_description: str = (
        "轻量备份：把「配置 + 记忆」导出成可移植的 .lbk 包。"
        "记忆按逻辑结构导出（记录/标签/关系/时间线 + 1024 维向量），"
        "剔掉 chroma 的运行态垃圾后体积约为全量的 1/8；配置里的密钥默认脱敏。"
        "自带校验：每条包逐项核对哈希、条数、引用完整性与向量长度。"
    )
    plugin_version: str = "1.0.0"
    configs: list[type] = [LightBackupConfig]

    def __init__(self, config: Any = None) -> None:
        """初始化插件。

        Args:
            config: 插件配置实例。
        """
        super().__init__(config)
        self._schedule_ids: list[str] = []
        self._startup_task: Any = None

    def get_components(self) -> list[type]:
        """返回组件（插件被关掉时整体下线）。"""
        if isinstance(self.config, LightBackupConfig) and not self.config.plugin.enabled:
            return []
        return [LightBackupService, BackupCommand]

    async def on_plugin_loaded(self) -> None:
        """插件加载完成：按配置注册定时巡检，必要时启动后先备一份。"""
        if not isinstance(self.config, LightBackupConfig):
            logger.warning("[light_backup] 配置对象不符合预期，只注册命令")
            return
        if not self.config.plugin.enabled:
            logger.info("[light_backup] 插件已关闭（plugin.enabled = false）")
            return

        schedule_cfg = self.config.schedule
        if schedule_cfg.enabled:
            self._startup_task = asyncio.create_task(self._register_schedule_when_ready())
        else:
            logger.info("[light_backup] 自动备份已关闭，只能手动 /备份 现在")

        if schedule_cfg.run_on_startup:
            asyncio.create_task(self._startup_backup())

    async def on_unload(self) -> None:
        """插件卸载：回收后台任务。"""
        if self._startup_task is not None:
            self._startup_task.cancel()
            self._startup_task = None

    # ── 调度 ─────────────────────────────────────────────────────
    async def _register_schedule_when_ready(self) -> None:
        """等调度器就绪后注册巡检任务（调度器只有 Bot.start() 之后才可用）。"""
        try:
            scheduler = get_unified_scheduler()
        except Exception as error:  # noqa: BLE001 - 拿不到就不注册
            logger.warning(f"[light_backup] 调度器不可用，自动备份未注册：{error}")
            return

        for _ in range(_SCHEDULER_WAIT_ROUNDS):
            try:
                schedule_id = await scheduler.create_schedule(
                    callback=self._tick_job,
                    trigger_type=TriggerType.TIME,
                    trigger_config={"interval_seconds": _TICK_SECONDS},
                    is_recurring=True,
                    task_name=_TICK_TASK_NAME,
                    force_overwrite=True,
                )
            except RuntimeError:
                await asyncio.sleep(0.5)
                continue
            except Exception as error:  # noqa: BLE001 - 反复失败就放弃注册
                logger.warning(f"[light_backup] 注册巡检任务失败：{error}")
                await asyncio.sleep(2.0)
                continue

            self._schedule_ids = [schedule_id]
            logger.info(
                f"[light_backup] 自动备份已注册（每 {_TICK_SECONDS}s 巡检，"
                f"每天 {self.config.schedule.daily_at} 备份）"
            )
            return

        logger.warning("[light_backup] 等待调度器就绪超时，自动备份未注册")

    def _due(self, now: float, state: dict[str, Any]) -> bool:
        """现在该不该自动备份。"""
        schedule_cfg = self.config.schedule if isinstance(self.config, LightBackupConfig) else None
        if schedule_cfg is None or not schedule_cfg.enabled:
            return False

        parsed = _parse_daily_at(schedule_cfg.daily_at)
        if parsed is None:
            return False

        hour, minute = parsed
        local = time.localtime(now)
        now_minutes = local.tm_hour * 60 + local.tm_min
        if now_minutes < hour * 60 + minute:
            return False

        today = date(local.tm_year, local.tm_mon, local.tm_mday).isoformat()
        last_day = str(state.get("last_backup_day") or "")
        if last_day == today:
            return False

        last_at = float(state.get("last_backup_at") or 0.0)
        min_interval = float(getattr(schedule_cfg, "min_interval_hours", 6.0) or 0.0) * 3600.0
        if min_interval and last_at and now - last_at < min_interval:
            return False

        return True

    def _service(self) -> LightBackupService:
        """造一个服务实例（服务取的是插件配置，实例本身无状态）。"""
        return LightBackupService(self)

    async def _tick_job(self) -> None:
        """调度器回调：到点就备份。"""
        try:
            service = self._service()
            state = await service.load_state()
            now = time.time()
            if not self._due(now, state):
                return

            result = await service.export(reason="schedule")
            if not result.get("ok"):
                logger.error(f"[light_backup] 定时备份失败：{result.get('error')}")
                return

            # 标记「今天已经备过」（写进状态，防止巡检反复触发）
            local = time.localtime(now)
            today = date(local.tm_year, local.tm_mon, local.tm_mday).isoformat()
            state = await service.load_state()
            state["last_backup_day"] = today
            await service._save_state(state)  # noqa: SLF001 - 同一插件内部状态
        except Exception as error:  # noqa: BLE001 - 巡检失败不打断下一轮
            logger.error(f"[light_backup] 巡检异常：{error}")

    async def _startup_backup(self) -> None:
        """启动后先备一份。"""
        await asyncio.sleep(3.0)  # 让 bot 自己先站稳
        try:
            service = self._service()
            state = await service.load_state()
            now = time.time()
            min_interval = float(getattr(self.config.schedule, "min_interval_hours", 6.0) or 0.0)
            last_at = float(state.get("last_backup_at") or 0.0)
            if min_interval and last_at and now - last_at < min_interval * 3600.0:
                logger.info("[light_backup] 距上次备份太近，启动备份跳过")
                return
            result = await service.export(reason="startup")
            if not result.get("ok"):
                logger.warning(f"[light_backup] 启动备份失败：{result.get('error')}")
        except Exception as error:  # noqa: BLE001
            logger.warning(f"[light_backup] 启动备份异常：{error}")


__all__ = ["LightBackupPlugin"]
